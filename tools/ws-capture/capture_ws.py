#!/usr/bin/env python3
"""
Capture and analyse JUNG HOME gateway WebSocket traffic.

Read-only: this tool never sends a command frame, so it cannot change the state
of anything in your installation. It opens the same `wss://<host>/ws` session
the integration uses, records every frame with a receive timestamp, and can walk
you through a scripted set of button gestures so the recording is
self-describing.

Why it exists: the captures in `disk_dump/ws-capture*/` are raw frame dumps with
**no timestamps**, so they cannot answer the two questions still open in this
repo's backlog:

  * rockers — what edges does a real press/double/hold actually produce, on
    which channel, and with what timing? (what the integration's hold
    threshold and duplicate window rest on). The mechanism is now
    established (docs/cross-repo-analysis.md §1.1): the gateway synthesises the
    release, so a tap is a ~0.4 to 0.5 s pulse; device firmware 2.2.0.2 publishes
    every event twice ~1 s apart, so a tap arrives as TWO pairs and a hold as
    ONE; the second copy lands on the SAME channel on a rocker half and on the
    OTHER channel on a single-key element (the gateway toggles the side per
    reception — not an "echo"). What a capture still adds: the numbers for
    your firmware, and a single-key element has never been measured.
  * covers — does a moving blind stream intermediate `level` values, or only
    report the endpoint? (blocks the cover travel-state backlog item). Drive
    the blind from its WALL BUTTON, not from Home Assistant and not from the
    app: a move commanded through the gateway's API has `level` report the
    *target* for ~4 s before the device's own status catches up, which reads
    exactly like streamed positions and would answer the question wrongly.

Usage:

    # guided rocker session (recommended — prompts you through the gestures)
    python capture_ws.py capture --host 192.168.1.50 --script rocker

    # guided cover session
    python capture_ws.py capture --host 192.168.1.50 --script cover

    # free-form: just record everything for N seconds
    python capture_ws.py capture --host 192.168.1.50 --script none --seconds 120

    # read a capture back and print the analysis
    python capture_ws.py analyze disk_dump/ws-capture-<stamp>/frames.jsonl

The token is read from $JUNGHOME_TOKEN, --token-file, or an interactive prompt.
It is sent as a connect header and is NEVER written to the output file.

Output goes under disk_dump/ by default, which is gitignored — captures contain
your device labels and should not be committed.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import os
import ssl
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import aiohttp

# Datapoint types a rocker reports, and how the integration reads their value.
ROCKER_TYPES = ("up_request", "down_request", "trigger_request")
# Cover position/tilt datapoint types.
COVER_TYPES = ("level", "angle")

# What the analysis measures a capture against: the integration's gesture
# constants (custom_components/junghome/const.py — BUTTON_HOLD_THRESHOLD and
# BUTTON_DUPLICATE_WINDOW, in seconds) and the shipped blueprint's legacy
# double-click window default (blueprints/automation/junghome/
# button_gestures.yaml, `double_click_window`, in ms there). Mirrored rather
# than imported so the tool runs without Home Assistant installed; a test in
# tests/test_blueprint.py pins them to the real values.
HOLD_THRESHOLD_S = 1.0
DUPLICATE_WINDOW_S = 1.2
LEGACY_DOUBLE_CLICK_WINDOW_S = 1.0

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# --------------------------------------------------------------------------
# capture
# --------------------------------------------------------------------------


def _resolve_token(args: argparse.Namespace) -> str:
    if args.token_file:
        return Path(args.token_file).read_text(encoding="utf-8").strip()
    env = os.environ.get("JUNGHOME_TOKEN")
    if env:
        return env.strip()
    print("Gateway token not found in $JUNGHOME_TOKEN or --token-file.")
    print("Find it in Home Assistant: .storage/core.config_entries -> junghome.")
    try:
        return input("Paste the gateway token: ").strip()
    except (EOFError, KeyboardInterrupt):
        sys.exit("no token given")


class Recorder:
    """Writes timestamped frame/marker records and echoes interesting ones."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = path.open("w", encoding="utf-8")
        self._start = time.monotonic()
        self.marker: str | None = None
        self.frames = 0

    def _write(self, record: dict[str, object]) -> None:
        record["elapsed"] = round(time.monotonic() - self._start, 4)
        record["wall"] = datetime.now(tz=UTC).isoformat()
        self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._fh.flush()

    def mark(self, marker: str | None, note: str = "") -> None:
        self.marker = marker
        self._write({"kind": "marker", "marker": marker, "note": note})

    def frame(self, raw: str) -> None:
        self.frames += 1
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            self._write({"kind": "unparsed", "marker": self.marker, "raw": raw})
            return
        self._write({"kind": "frame", "marker": self.marker, "frame": parsed})
        self._echo(parsed)

    def _echo(self, frame: dict) -> None:
        """Print a one-line summary of edges worth watching live."""
        if frame.get("type") != "datapoint":
            return
        data = frame.get("data")
        if not isinstance(data, dict):
            return
        dp_type = data.get("type")
        if dp_type not in (*ROCKER_TYPES, *COVER_TYPES):
            return
        values = data.get("values") or []
        pairs = " ".join(
            f"{v.get('key')}={v.get('value')}" for v in values if isinstance(v, dict)
        )
        elapsed = round(time.monotonic() - self._start, 3)
        print(f"  [{elapsed:8.3f}s] {data.get('id')} {dp_type}: {pairs}")

    def close(self) -> None:
        self._fh.close()


# Scripted gesture sequences. Each step is (marker, instruction).
SCRIPTS: dict[str, list[tuple[str, str]]] = {
    "rocker": [
        (
            "idle",
            (
                "Do NOT touch anything. This records background chatter for 10 s\n"
                "    so real edges can be told apart from noise."
            ),
        ),
        (
            "single-a",
            (
                "Press and release the button ONCE, quickly. Repeat 3 times,\n"
                "    pausing ~2 s between presses."
            ),
        ),
        (
            "double-a",
            (
                "DOUBLE-click the same button (as fast as you naturally would).\n"
                "    Repeat 3 times, pausing ~2 s between them."
            ),
        ),
        (
            "hold-a",
            (
                "HOLD the same button for about 3 seconds, then release.\n"
                "    Repeat 3 times."
            ),
        ),
        (
            "single-b",
            (
                "Now the OTHER side of the same rocker: press and release once.\n"
                "    Repeat 3 times. (On a rocker each side has its own channel;\n"
                "    on a single-key element the two copies of one tap alternate\n"
                "    channels instead — this step tells the two apart.)"
            ),
        ),
        (
            "alternate",
            (
                "Alternate sides: press A, then B, then A, then B — about 1 s\n"
                "    apart. (Deliberate alternation, to compare against the\n"
                "    firmware's own doubled copies ~1 s apart.)"
            ),
        ),
    ],
    "cover": [
        ("idle", "Do NOT touch anything for 10 s (background chatter)."),
        (
            "close-full",
            (
                "Fully CLOSE the blind from its WALL BUTTON — not from Home\n"
                "    Assistant and not from the app (a move commanded through the\n"
                "    gateway's API reports the TARGET level for ~4 s, which would\n"
                "    look like streamed positions). Let it run all the way to the\n"
                "    end. Wait until it stops."
            ),
        ),
        (
            "open-full",
            (
                "Fully OPEN the blind and let it run to the end. Wait until it\n"
                "    stops."
            ),
        ),
        (
            "partial-stop",
            "Start closing, then STOP it halfway.",
        ),
        (
            "tilt",
            (
                "If this blind has slats: tilt them one way, then the other.\n"
                "    (Skip with Enter if it has no tilt.)"
            ),
        ),
    ],
}


async def _reader(ws: aiohttp.ClientWebSocketResponse, rec: Recorder) -> None:
    async for msg in ws:
        if msg.type == aiohttp.WSMsgType.TEXT:
            rec.frame(msg.data)
        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            break


async def _run_script(rec: Recorder, steps: list[tuple[str, str]]) -> None:
    print("\nThe gateway sends its full device list on connect; that is recorded")
    print("first. Then follow each step and press Enter when you have finished it.\n")
    for index, (marker, instruction) in enumerate(steps, start=1):
        rec.mark(marker, instruction.replace("\n", " "))
        print(f"\n--- step {index}/{len(steps)}: {marker} ---")
        print(f"    {instruction}")
        if marker == "idle":
            print("    (waiting 10 s automatically...)")
            await asyncio.sleep(10)
        else:
            await asyncio.to_thread(input, "    ...press Enter when done: ")
    rec.mark(None, "script complete")


async def _capture(args: argparse.Namespace) -> int:
    token = _resolve_token(args)
    out_dir = Path(args.out) if args.out else None
    if out_dir is None:
        stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        out_dir = REPO_ROOT / "disk_dump" / f"ws-capture-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "frames.jsonl"

    # The gateway serves a self-signed certificate, exactly as the integration's
    # own client does (async_get_clientsession(verify_ssl=False)).
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE

    rec = Recorder(path)
    print(f"Recording to {path}")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(
                f"wss://{args.host}/ws",
                headers={"token": token},
                ssl=ssl_ctx,
                heartbeat=30,
            ) as ws:
                print(f"Connected to {args.host}.")
                reader = asyncio.create_task(_reader(ws, rec))
                try:
                    steps = SCRIPTS.get(args.script)
                    if steps:
                        await _run_script(rec, steps)
                    else:
                        print(
                            f"Free-form capture for {args.seconds} s. Ctrl-C to stop."
                        )
                        await asyncio.sleep(args.seconds)
                finally:
                    reader.cancel()
    except aiohttp.WSServerHandshakeError as err:
        if err.status in (401, 403):
            return _fail(f"gateway rejected the token (HTTP {err.status})")
        return _fail(f"handshake failed: {err}")
    except (aiohttp.ClientError, OSError) as err:
        return _fail(f"could not connect to {args.host}: {err}")
    except asyncio.CancelledError:
        # Ctrl-C / SIGTERM cancel the task rather than raising KeyboardInterrupt
        # inside it. Every record was flushed as it was written, so the capture
        # on disk is complete up to this moment — close and print the summary
        # instead of dying with a traceback.
        print("\nstopped early — capture saved")
    finally:
        rec.close()
    print(f"\nRecorded {rec.frames} frames to {path}")
    print("Now run:")
    print(f"    python {Path(__file__).name} analyze {path}")
    return 0


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 1


# --------------------------------------------------------------------------
# analyze
# --------------------------------------------------------------------------


def _load(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _edges(records: list[dict], types: tuple[str, ...]) -> list[dict]:
    """Flatten datapoint frames of the given types into edge rows."""
    rows = []
    for rec in records:
        if rec.get("kind") != "frame":
            continue
        frame = rec.get("frame") or {}
        if frame.get("type") != "datapoint":
            continue
        data = frame.get("data")
        if not isinstance(data, dict) or data.get("type") not in types:
            continue
        values = {
            v.get("key"): v.get("value")
            for v in (data.get("values") or [])
            if isinstance(v, dict)
        }
        rows.append(
            {
                "elapsed": rec.get("elapsed"),
                "marker": rec.get("marker"),
                "id": data.get("id"),
                "type": data.get("type"),
                "values": values,
            }
        )
    return rows


# Auto-segmentation for captures recorded without script markers (e.g. when
# the operator cannot answer the interactive prompts). A silence longer than
# GROUP_SILENCE_S starts a new instructed group; edges within GESTURE_GAP_S of
# each other belong to the same physical gesture. Both were validated against
# a real labelled session: instructed pauses were 15 s+, and no gap inside a
# genuine gesture burst exceeded ~1.1 s.
GROUP_SILENCE_S = 10.0
GESTURE_GAP_S = 2.0


def _auto_group(rows: list[dict]) -> bool:
    """Assign synthetic group markers to a marker-less capture by silence.

    Returns True when auto-grouping was applied (so the report can say the
    labels are inferred, not scripted). A capture with real markers is left
    untouched.
    """
    if not rows or any(r["marker"] for r in rows):
        return False
    group = 0
    previous = None
    for row in rows:
        if previous is None or row["elapsed"] - previous > GROUP_SILENCE_S:
            group += 1
        row["marker"] = f"group-{group}"
        previous = row["elapsed"]
    return True


def _split_gestures(group: list[dict]) -> list[list[dict]]:
    """Split one group's edges into physical gestures on GESTURE_GAP_S.

    Never split after an unanswered press: a hold's pulse (measured 2.4-3.1 s)
    exceeds the gap threshold, but its release still belongs to the same
    gesture — a gesture can only end on a release.
    """
    gestures: list[list[dict]] = []
    current: list[dict] = []
    for row in group:
        if (
            current
            and row["elapsed"] - current[-1]["elapsed"] > GESTURE_GAP_S
            and current[-1]["values"].get(current[-1]["type"]) != "1"
        ):
            gestures.append(current)
            current = []
        current.append(row)
    if current:
        gestures.append(current)
    return gestures


def _print_burst_shapes(by_marker: dict[str | None, list[dict]]) -> None:
    """Report presses-per-gesture — the doubled-reporting diagnostic.

    1.00 presses per gesture is clean reporting; 2.00 is the doubled-burst
    device firmware (one physical tap emitted as two press/release pairs), on
    which single and double clicks are indistinguishable. This single number
    is what decides which gesture strategies can work at all.
    """
    print("\n=== BURST SHAPE (presses per physical gesture) ===")
    for marker, group in by_marker.items():
        counts = [
            sum(1 for r in g if r["values"].get(r["type"]) == "1")
            for g in _split_gestures(group)
        ]
        if not counts:
            continue
        avg = sum(counts) / len(counts)
        note = ""
        if all(c == 2 for c in counts):
            note = "  <- every gesture doubled (affected device firmware)"
        elif all(c == 1 for c in counts):
            note = "  <- clean single-pair reporting"
        print(f"-- {marker or '(unmarked)'}: {counts}  avg {avg:.2f}{note}")


def _press_durations(subset: list[dict], rows: list[dict]) -> list[float]:
    """Press->release duration for each press in ``subset``."""
    out = []
    for press in (r for r in subset if r["values"].get(r["type"]) == "1"):
        release = next(
            (
                r
                for r in rows
                if r["id"] == press["id"]
                and r["elapsed"] > press["elapsed"]
                and r["values"].get(r["type"]) == "0"
            ),
            None,
        )
        if release:
            out.append(release["elapsed"] - press["elapsed"])
    return out


def _press_gaps(subset: list[dict]) -> list[float]:
    """Gap between consecutive presses within ``subset``."""
    presses = [r for r in subset if r["values"].get(r["type"]) == "1"]
    return [b["elapsed"] - a["elapsed"] for a, b in itertools.pairwise(presses)]


def _print_edges(by_marker: dict[str | None, list[dict]]) -> None:
    for marker, group in by_marker.items():
        print(f"\n-- {marker or '(unmarked)'} — {len(group)} edges")
        previous = None
        for row in group:
            value = row["values"].get(row["type"])
            edge = "PRESS  " if value == "1" else "release"
            gap = "" if previous is None else f"  (+{row['elapsed'] - previous:.3f}s)"
            print(f"   {row['elapsed']:8.3f}s  {edge}  {row['type']:<16}{gap}")
            previous = row["elapsed"]

        ids = {row["id"] for row in group}
        types = {row["type"] for row in group}
        if len(types) > 1:
            print(f"   NOTE: {len(types)} channels fired here: {sorted(types)}")
            print(
                "         -> a single-key element (the firmware's two copies of "
                "one tap alternate up/down), or you pressed both sides."
            )
        if len(ids) > 1:
            print(f"   datapoint ids seen: {sorted(ids)}")


def _print_timings(by_marker: dict[str | None, list[dict]], rows: list[dict]) -> None:
    """Report durations/gaps PER GESTURE and check the gesture constants.

    Per gesture, not pooled: a double-click's press->press gap and two
    deliberately separate presses are both "gaps", and mixing them produces a
    range wide enough to justify any window at all.
    """
    print("\n=== TIMING SUMMARY (what the gesture constants depend on) ===")
    reported = False
    for marker, group in by_marker.items():
        durations, gaps = _press_durations(group, rows), _press_gaps(group)
        if not durations and not gaps:
            continue
        reported = True
        print(f"\n-- {marker or '(unmarked)'}")
        if durations:
            print(
                f"   press->release: min {min(durations):.3f}s  "
                f"max {max(durations):.3f}s  n={len(durations)}"
            )
        if gaps:
            print(
                f"   press->press:   min {min(gaps):.3f}s  "
                f"max {max(gaps):.3f}s  n={len(gaps)}"
            )
    if not reported:
        print("Not enough paired edges to derive timings.")
        return

    singles = by_marker.get("single-a") or []
    doubles = by_marker.get("double-a") or []
    ordinary = _press_durations(singles, rows) + _press_durations(doubles, rows)
    held = _press_durations(by_marker.get("hold-a") or [], rows)
    copy_gaps = _copy_gaps(singles, rows)
    double_gaps = _click_gaps(doubles)

    print("\n--- against the integration's gesture constants ---")
    if ordinary and held:
        verdict = "OK" if max(ordinary) < HOLD_THRESHOLD_S < min(held) else "WRONG"
        print(
            f"   hold threshold (BUTTON_HOLD_THRESHOLD = {HOLD_THRESHOLD_S} s): "
            f"taps end by {max(ordinary):.3f}s and holds last {min(held):.3f}s+ "
            f" -> {verdict}."
        )
    elif ordinary:
        print(
            f"   hold threshold (BUTTON_HOLD_THRESHOLD = {HOLD_THRESHOLD_S} s): "
            f"taps end by {max(ordinary):.3f}s; no hold gesture captured to "
            "bound the other side."
        )
    if copy_gaps:
        verdict = "OK" if max(copy_gaps) <= DUPLICATE_WINDOW_S else "TOO SHORT"
        print(
            f"   duplicate window (BUTTON_DUPLICATE_WINDOW = {DUPLICATE_WINDOW_S} s): "
            f"the firmware's copy of a tap presses {min(copy_gaps):.3f}-"
            f"{max(copy_gaps):.3f}s after the first release  -> {verdict}."
        )
    else:
        print(
            "   duplicate window (BUTTON_DUPLICATE_WINDOW = "
            f"{DUPLICATE_WINDOW_S} s): single taps arrive as one pair here, so "
            "there is no copy to measure (turn suppression off only on such "
            "firmware)."
        )
    if double_gaps:
        verdict = (
            "OK" if max(double_gaps) < LEGACY_DOUBLE_CLICK_WINDOW_S else "TOO SHORT"
        )
        print(
            "   legacy double-click window (blueprint default "
            f"{LEGACY_DOUBLE_CLICK_WINDOW_S * 1000:.0f} ms): the second click "
            f"lands {max(double_gaps):.3f}s after the first  -> {verdict}. Only "
            "meaningful on firmware that reports each tap ONCE (see BURST "
            "SHAPE); with doubled taps a double-click is indistinguishable "
            "from a single one."
        )
    else:
        print("   legacy double-click window: no double-click gestures captured.")


def _copy_gaps(subset: list[dict], rows: list[dict]) -> list[float]:
    """First release -> second press, per single-tap gesture reported twice.

    The integration's duplicate window is measured from the release that
    completed a click to the next press on the device, so that is the gap
    that must fit inside ``BUTTON_DUPLICATE_WINDOW``. Gestures with fewer than
    two presses (clean single-pair firmware) contribute nothing.
    """
    out = []
    for gesture in _split_gestures(subset):
        presses = [r for r in gesture if r["values"].get(r["type"]) == "1"]
        if len(presses) < 2:
            continue
        release = next(
            (
                r
                for r in rows
                if r["id"] == presses[0]["id"]
                and r["elapsed"] > presses[0]["elapsed"]
                and r["values"].get(r["type"]) == "0"
            ),
            None,
        )
        if release and presses[1]["elapsed"] > release["elapsed"]:
            out.append(presses[1]["elapsed"] - release["elapsed"])
    return out


def _click_gaps(subset: list[dict]) -> list[float]:
    """First release -> second release, per double-click gesture.

    A click fires at the release, and the blueprint's legacy double-click
    wait starts at the first click and needs the second inside the window —
    so release-to-release is the gap ``double_click_window`` has to cover.
    """
    out = []
    for gesture in _split_gestures(subset):
        releases = [r for r in gesture if r["values"].get(r["type"]) == "0"]
        if len(releases) >= 2:
            out.append(releases[1]["elapsed"] - releases[0]["elapsed"])
    return out


def _analyze_rocker(records: list[dict]) -> None:
    rows = _edges(records, ROCKER_TYPES)
    print("\n=== ROCKER EDGES ===")
    if not rows:
        print("No rocker edges captured. Did you press the button during a step?")
        return

    if _auto_group(rows):
        print(
            f"(no script markers in this capture — groups inferred from "
            f">{GROUP_SILENCE_S:.0f}s silences; keep instructed pauses long)"
        )

    by_marker: dict[str | None, list[dict]] = {}
    for row in rows:
        by_marker.setdefault(row["marker"], []).append(row)

    _print_edges(by_marker)
    _print_burst_shapes(by_marker)
    _print_timings(by_marker, rows)


def _analyze_cover(records: list[dict]) -> None:
    rows = _edges(records, COVER_TYPES)
    print("\n=== COVER LEVEL / ANGLE FRAMES ===")
    if not rows:
        print("No level/angle frames captured.")
        return
    print(
        "   CAVEAT: only a move started from the blind's WALL BUTTON answers the\n"
        "   streaming question. A move commanded through the gateway's API (Home\n"
        "   Assistant, or the app when it goes via the gateway) reports the TARGET\n"
        "   level for ~4 s before the device's own status catches up — that is a\n"
        "   second frame, not a streamed position."
    )
    by_marker: dict[str | None, list[dict]] = {}
    for row in rows:
        by_marker.setdefault(row["marker"], []).append(row)
    for marker, group in by_marker.items():
        print(f"\n-- {marker or '(unmarked)'} — {len(group)} frames")
        for row in group:
            print(f"   {row['elapsed']:8.3f}s  {row['type']:<6} {row['values']}")
        levels = [r for r in group if r["type"] == "level"]
        if len(levels) > 2:
            print(
                f"   -> {len(levels)} level frames during this move: the gateway "
                "DOES stream intermediate positions (if the move was wall-button "
                "driven — see the caveat above)."
            )
        elif levels:
            print(
                f"   -> only {len(levels)} level frame(s): the gateway reports "
                "the endpoint, not the travel."
            )
        moves = {
            r["values"].get("level_move") for r in levels if "level_move" in r["values"]
        }
        if moves:
            print(f"   level_move values seen: {sorted(m for m in moves if m)}")


def _analyze(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        return _fail(f"no such capture: {path}")
    records = _load(path)
    frames = [r for r in records if r.get("kind") == "frame"]
    print(f"{path}: {len(records)} records, {len(frames)} frames")
    types: dict[str, int] = {}
    for rec in frames:
        frame_type = (rec.get("frame") or {}).get("type", "?")
        types[frame_type] = types.get(frame_type, 0) + 1
    print("frame types:", ", ".join(f"{k}={v}" for k, v in sorted(types.items())))
    _analyze_rocker(records)
    _analyze_cover(records)
    print(
        "\nShare this analysis (not the raw capture — it contains your device "
        "labels) when reporting findings."
    )
    return 0


# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    cap = sub.add_parser("capture", help="record a live session")
    cap.add_argument("--host", required=True, help="gateway IP or hostname")
    cap.add_argument(
        "--script",
        choices=[*SCRIPTS, "none"],
        default="rocker",
        help="guided gesture script to walk through (default: rocker)",
    )
    cap.add_argument(
        "--seconds", type=int, default=120, help="duration for --script none"
    )
    cap.add_argument("--out", help="output directory (default: disk_dump/ws-capture-*)")
    cap.add_argument("--token-file", help="file containing the gateway token")

    ana = sub.add_parser("analyze", help="summarise a capture")
    ana.add_argument("path", help="path to frames.jsonl")

    args = parser.parse_args()
    if args.command == "capture":
        try:
            return asyncio.run(_capture(args))
        except KeyboardInterrupt:
            # asyncio.run re-raises KeyboardInterrupt after cancelling the
            # task; _capture already closed the recorder and printed the
            # summary on its CancelledError path.
            return 0
    return _analyze(args)


if __name__ == "__main__":
    sys.exit(main())
