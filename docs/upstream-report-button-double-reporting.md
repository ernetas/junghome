# Report to JUNG: push-button events are delivered twice on device firmware 2.2.0.x

Draft, 2026-09-16, for JUNG HOME support / the gateway firmware team. Everything
below was established on a JUNG HOME Gateway running **2.1.3 (build 2840, API
1.5.0)** with push buttons on device firmware **2.2.0.2**, and is reproducible
with the tools in this repository.

## Summary

Since app release 2.2.0 (which updated the push-button device firmware to
2.2.0.x), every button *click* reaches local-API clients as **two** complete
press/release pairs about a second apart, and a *hold* as one. Before the
update the same buttons produced one pair per click (the gateway's own
archived logs, 2026-06-20 → 07-28, ~450 clicks: 1.00 pairs per click; the
gateway firmware did not change across that window). Single and double clicks
are therefore indistinguishable to any client of the local API, and every
client has to guess with a time window.

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
   two pairs. A hold (2/3 or 6) sets a state the second copy leaves
   unchanged, so the second copy is suppressed by the value-and-mode check
   and a hold stays one pair.
3. **Single-key elements alternate sides.** For events 5/6 the reported side
   is the toggle of a *service-wide* `_prevButtonType` ("for downwards
   compatibility"), so the two copies of one tap land on `up_request` and
   `down_request` alternately, and a hold's second copy can land on the
   other datapoint while the first is still down (captured live, 1 of 4
   holds).

## Evidence available on request

- Timestamped WebSocket captures of scripted gestures on rocker and single-key
  elements (2026-08-02 and 2026-09-16): tap pulse 0.40–0.53 s (the
  synthesised release), second copy 0.11–1.03 s after the first release, hold
  pulse 2.4–3.1 s (the finger). Table in `docs/gateway-websocket.md`.
- The gateway's own `/devices/?verbose=true` output showing
  `software_revision: [2, 2, 0, 2]` on every push button.
- The on-air capture from the Bluetooth-direct sibling project (same counter,
  fresh SEQ, ~1 s apart).

## Suggested fixes

- **Gateway (one line):** de-duplicate on the `0x5012` counter byte per
  element in `btmesh_property_service.js` — drop a `KEY_EVT` whose counter
  equals the last one seen from that element within ~2 s. This alone restores
  one pair per click for every API client without touching the devices.
- **Device firmware:** publish each `KEY_EVT` once — or, if the second
  publication is intentional redundancy, increment the counter so receivers
  can tell a repeat from a new event (today the counter is the only field
  that could, and it does not).
- **API:** consider exposing the gesture the device already reports (click /
  hold-start / release) instead of flattening it into synthesised edges; the
  device knows, the gateway knows, and clients currently reconstruct it from
  pulse width.

## Contact / repository

Integration: https://github.com/ernetas/junghome (Home Assistant custom
integration for the JUNG HOME Gateway, local API). The capture tool is
`tools/ws-capture/capture_ws.py`.
