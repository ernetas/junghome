# WebSocket capture

`capture_ws.py` records the gateway's WebSocket traffic **with timestamps** and
walks you through a scripted set of gestures, so the recording can answer
questions the existing `disk_dump/ws-capture*/` dumps cannot (they are raw
frames with no timing and no idea what the user was doing).

It is **read-only** — it never sends a command frame, so it cannot change the
state of anything in your installation.

## What it's for

Two things in this repo need real timing evidence:

1. **Rocker buttons.** The shipped blueprint derives single/double/hold from raw
   `pressed`/`depressed` edges. The mechanism behind what those edges look
   like is established (see §1.1 of
   [docs/cross-repo-analysis.md](../../docs/cross-repo-analysis.md)): the
   gateway synthesises the release, so a tap is a ~0.4–0.5 s pulse, and device
   firmware 2.2.0.2 publishes every event twice ~1 s apart, so a tap arrives as
   **two** pairs and a hold as **one**. The second copy lands on the *same*
   channel on a rocker half and on the *other* channel on a single-key element
   (the gateway toggles the side on each reception — there is no echo). A
   capture measures your own firmware's numbers; a single-key element has
   never been captured at all.
2. **Cover travel states.** Whether intermediate positions stream during a
   move has never been observed — that decides whether a cover can track
   position live or only jump to the target. (`level_move` is *not* the
   answer: the firmware slices the wrong octets, so it is always `0`.)
   **Drive the blind from its wall button**, never from Home Assistant or the
   app: a move commanded through the gateway's API reports the *target* level
   for ~4 s before the device's own status catches up, which looks exactly
   like a streamed position and would answer the question wrongly.

## Running it

```sh
pip install aiohttp                        # the only dependency

export JUNGHOME_TOKEN='<your gateway token>'
python capture_ws.py capture --host 192.168.1.50 --script rocker
```

The token is the one the integration already holds — find it in Home
Assistant's `.storage/core.config_entries` under the `junghome` entry. It is
sent as a connect header and is **never written to the output file**. You can
also pass `--token-file path` or let it prompt.

The session prints each step, waits for you to press Enter when you've done it,
and echoes matching edges live so you can see the gateway reacting:

```
--- step 2/6: single-a ---
    Press and release the button ONCE, quickly. Repeat 3 times,
    pausing ~2 s between presses.
  [  12.418s] id7ddb371a88bbd01-00c up_request: up_request=1
  [  12.533s] id7ddb371a88bbd01-00c up_request: up_request=0
    ...press Enter when done:
```

Scripts available: `--script rocker` (default), `--script cover`, or
`--script none --seconds N` for a free-form recording.

For the rocker script, pick **one physical rocker** and use the same button
throughout — "button A" means the same side every time, "button B" its
sibling. The `single-b` step is what tells a rocker (each side its own
channel) from a single-key element (the two copies of one tap alternate
channels); `alternate` is deliberate ~1 s alternation to compare against the
firmware's own doubled copies.

## Reading it back

```sh
python capture_ws.py analyze disk_dump/ws-capture-<stamp>/frames.jsonl
```

This prints the edges per gesture, flags when more than one channel fired
inside a single gesture (a single-key element's alternating copies, or both
sides pressed), and derives the timing bounds the blueprint defaults depend
on — press→release durations and press→press gaps, reported **per gesture**
rather than pooled, because a double-click gap and two deliberately separate
presses are both "gaps" and mixing them would justify any window at all.

It also reports the **burst shape** — presses per physical gesture — which is
the doubled-reporting diagnostic: 1.00 is clean, 2.00 is the affected device
firmware on which single and double clicks are indistinguishable (see the
rocker section of [gateway-websocket.md](../../docs/gateway-websocket.md) for
the 2026-08-02 measurements and the regression evidence). A capture recorded
**without** script markers is segmented automatically: >10 s silences separate
instructed groups, so if you can't use the interactive script, just leave
15-second hands-off pauses between gesture groups.

For cover captures it reports how many `level` frames arrived during each
move — more than a couple means the gateway streams intermediate positions,
**provided the move was started from the wall button** (an API-driven move
adds a target-level frame that is not a position; the output repeats this
caveat) — plus the `level_move` values seen.

## Output and privacy

Captures land in `disk_dump/ws-capture-<timestamp>/frames.jsonl`, which is
gitignored: the frames contain your **device labels**, and the connect
handshake includes the gateway's full function list. **Don't commit a capture
or paste one into a public issue** — share the `analyze` output instead, which
is timings and datapoint types rather than your home's inventory.

The record format is one JSON object per line:

```jsonc
{"kind": "marker", "marker": "single-a", "note": "...", "elapsed": 10.0, "wall": "..."}
{"kind": "frame",  "marker": "single-a", "frame": {...}, "elapsed": 12.4, "wall": "..."}
```

`elapsed` is monotonic seconds since the capture started (immune to clock
adjustments — this is the field the analysis uses); `wall` is the UTC wall
clock for correlating against Home Assistant logs.
