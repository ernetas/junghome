# Report to JUNG: push-button events are delivered twice on device firmware 2.2.0.x

Draft, 2026-09-16, for JUNG HOME support / the gateway firmware team. Everything
below was established on a JUNG HOME Gateway running **2.1.3 (build 2840, API
1.5.0)** with push buttons on device firmware **2.2.0.2**, and is reproducible
with the tools in this repository.

## Summary

Since app release 2.2.0 (which updated the device firmware to 2.2.0.x), every
button *click* reaches local-API clients as **two** complete press/release
pairs about a second apart, and a rocker *hold* as one. The gateway's own
middleware log dates the change (archived support snapshot, 2026-06-20 →
07-29, gateway firmware unchanged throughout, 354 click events and 73 holds):
it records the buttons' `software_revision` changing from v2.0.0.4 to
v2.2.0.2 on 2026-07-25/27 — every one of the 23 buttons in the project (a
24th had been removed from the project on 06-28) — and from each button's
update on, click events per burst go from **1.13 to 2.53** (bursts split at
a > 2 s gap; 89 % of the bursts before were a single click, none after).
The buttons in that log were all single-key elements (events 5/6/4); the
rocker behaviour below comes from WebSocket captures. Single and double clicks are
therefore indistinguishable to any client of the local API, and every client
has to guess with a time window.

## Mechanism

1. **The device publishes every access message twice.** An on-air capture
   (Bluetooth Mesh sniffer) shows each button event — vendor property
   `0x5012 KEY_EVT`, payload `[counter][event]` — sent twice, roughly 1 s
   apart, with a fresh network sequence number and the **same counter byte**.
   The CDB's publish-retransmit count is 0 on every model, so this is neither
   mesh retransmission nor configuration: the firmware itself publishes twice.
   OnOff statuses from the same devices show the same doubling.
2. **The gateway does not de-duplicate.** `services/btmesh_property_service.js`
   (middleware, lines ~184–256) reads the event byte with `Number(values[1])`
   and ignores the counter byte. Each copy of a click event (0/1 on rocker
   elements, 5 on single-key elements) is turned into a synthesised `[1, 0]`
   press/release pair (two 200 ms delays in the emitter), so a click becomes
   two pairs. A rocker hold (2/3) sets a state the second copy leaves
   unchanged, so that copy is suppressed by the value-and-mode check and the
   hold stays one pair. A single-key hold (6) is *not* protected this way —
   its copy is sent to the other side (point 3).
3. **Single-key elements alternate sides.** For events 5/6 the reported side
   is the toggle of a *service-wide* `_prevButtonType` ("for downwards
   compatibility"), so the two copies of one tap land on `up_request` and
   `down_request` alternately, and a hold's second copy can land on the
   other datapoint while the first is still down (captured live, 1 of 4
   holds; in the middleware log 10 of 25 single-key holds after the update,
   0 of 38 before).

## Evidence available on request

- Timestamped WebSocket captures of scripted gestures: rocker elements on
  2026-08-02 (tap pulse 0.40–0.53 s — the synthesised release — second copy
  0.11–1.03 s after the first release, hold pulse 2.44–3.11 s, the finger)
  and rocker plus single-key elements on 2026-09-16 (tap pulse 0.26–0.51 s,
  hold pulse 1.50–3.25 s; the copied single-key hold's copy 1.15 s). Tables
  in `docs/gateway-websocket.md`.
- The gateway's own middleware log (above): the `software_revision` change
  to v2.2.0.2 on all 23 buttons in the project, and the click-events-per-burst
  before and after it. (`/devices/?verbose=true` shows `[2, 2, 0, 2]` too, on
  each node's main-element function; the node's other functions read `null`
  — 18 of the 20 button functions in a 2026-09-16 probe — because of the
  separate gateway bug below, not because those devices went unread.)
- The on-air capture from the Bluetooth-direct sibling project (same counter,
  fresh SEQ, ~1 s apart).

## Suggested fixes

- **Gateway (one line):** de-duplicate on the `0x5012` counter byte per
  element in `btmesh_property_service.js` — drop a `KEY_EVT` whose counter
  equals the last one seen from that element within ~2 s. This alone restores
  one pair per click for every API client without touching the devices.
- **Device firmware:** publish each `KEY_EVT` once. If the second
  publication is intentional redundancy, keep its counter unchanged (as it
  is today — that is what makes the gateway fix above possible) and document
  that receivers must de-duplicate on it.
- **API:** consider exposing the gesture the device already reports (click /
  hold-start / release) instead of flattening it into synthesised edges; the
  device knows, the gateway knows, and clients currently reconstruct it from
  pulse width.

## A second, unrelated gateway bug: node properties on multi-function nodes

`software_revision`, a node-wide Generic Property on element 0, reads
`null` on every function of a multi-function node except
the one at the node's main element — 22 of 49 functions in the 2026-09-16
probe, 18 of them buttons. The middleware means to handle exactly this: it
skips polling such a property on non-main functions and copies the main
function's value to them (`[POLL-SKIP]`, `[PROP-REDIRECT]` and
`[PROP-SYNC]` in `services/device_state_service.js`). But
`Product.isDeviceAtMainElement` (`models/product.js`) also matches on the
function's *property* addresses, and every function's property states sit at
element 0 — so every function counts as the main one and polls the property
itself. The node answers; the answer is matched to the first function with
a *state* at that address (`handler/bt_event_handler.js`, the
`STATUS_LBC_PROP_STATUS_ID` case), so the other function's request times out
— every hour, for every such function (~850–900 `no answer from …` warnings
each in the June–July middleware log) — and after the allowed failures its
value is reset to `NaN` (`null` in the API) until the next `[PROP-SYNC]`,
which only happens when the main function's value changes. Suggested fix:
decide "main element" from the function's *states* only (as the answer
matching already does), so the skip and the copy take effect.

## Contact / repository

Integration: https://github.com/ernetas/junghome (Home Assistant custom
integration for the JUNG HOME Gateway, local API). The capture tool is
`tools/ws-capture/capture_ws.py`.
