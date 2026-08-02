# WebSocket capture

`capture_ws.py` records the gateway's WebSocket traffic **with timestamps** and
walks you through a scripted set of gestures, so the recording can answer
questions the existing `disk_dump/ws-capture*/` dumps cannot (they are raw
frames with no timing and no idea what the user was doing).

It is **read-only** — it never sends a command frame, so it cannot change the
state of anything in your installation.

## What it's for

Two things in this repo are blocked on real timing evidence:

1. **Rocker buttons.** The shipped blueprint derives single/double/hold from raw
   `pressed`/`depressed` edges, and its defaults (2 s hold, 0.4 s double-click
   window) plus the "one channel can echo the other" guidance in
   [docs/example-button-automation.md](../../docs/example-button-automation.md)
   rest on field reports rather than a measured capture.
2. **Cover travel states.** The firmware computes an opening/closing/stopped
   mode and puts `level_move` in every `level` datapoint
   (`PositionState.fromMeshMessage`), so travel *direction* is available — but
   whether intermediate positions actually stream during a move has never been
   observed. That decides whether a cover can track position live or only jump
   to the target.

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
sibling. The `alternate` step is what exposes a sibling-channel echo.

## Reading it back

```sh
python capture_ws.py analyze disk_dump/ws-capture-<stamp>/frames.jsonl
```

This prints the edges per gesture, flags when more than one channel fired
inside a single gesture (echo or genuine alternation), and derives the timing
bounds the blueprint defaults depend on — press→release durations and
press→press gaps, reported **per gesture** rather than pooled, because a
double-click gap and two deliberately separate presses are both "gaps" and
mixing them would justify any window at all.

For cover captures it reports how many `level` frames arrived during each
move — more than a couple means the gateway streams intermediate positions —
plus the `level_move` values seen.

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
