"""The gateway's health log (``GET /healthstatus/``) and the repair issues it drives.

Firmware v2.1.3 (build 2840), ``disk_dump/jung-20260801/sdb2/opt``:

- The route is ``api-server/dist/server.js:128`` behind ``auth()`` with no
  role, so the integration's registered token reads it like ``/functions/``
  (``api-middleware/auth.js`` answers 401 for a missing or unknown token,
  never 403). ``controllers/10_healthstatus-controller.js`` returns
  ``JungHealthStatusService.getAllErrors()``: the list the middleware last
  published over its IPC ``gateway_errors`` topic, replaced wholesale on each
  publish (``services/jung-healthstatus-service.js``). The WebSocket
  broadcast of it is commented out (``websocket-server-service.js``
  ``_on_error_event``), so polling is the only way to see it.
- The middleware side (``middleware/dist/services/health_status_service.js``)
  is an **append-only, in-memory log since the middleware started**: every
  ``debug``/``info``/``warn``/``error`` call pushes
  ``{level, time, description, details}`` and republishes the whole list,
  newest first (``[...list].reverse()``). Nothing ever removes an entry — a
  condition "clears" only when the gateway restarts (empty list) or when a
  later entry supersedes it. ``level`` is ``DEBUG``/``INFO``/``WARN``/
  ``ERROR``; ``time`` is ``Date.toISOString()`` (UTC — the api-server DTO's
  "European String" comment and the ``/apidoc`` schema are wrong); ``details``
  is normally a string (the fixed default text when the caller gave none), but
  two startup failures pass a caught error through, which can arrive as a
  JSON object.

Which messages this integration turns into repair issues is the table below;
every other message (cloud, provisioning, IV update, CPU load, update history)
is only kept for diagnostics. ``JUNG HOME Devices are unreachable`` is
deliberately NOT an issue: it is the middleware's ``isDeviceOnline``
(``devices_service.js:158,174``), which reads push buttons as unreachable while
they work (CLAUDE.md, settled decision on reachability).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from homeassistant.helpers import issue_registry as ir

# How often, in seconds, the health log is re-read after the setup-time read:
# four times an hour. The log only matters for conditions that persist (a dead
# Bluetooth chip, a gateway out of sequence numbers, a day without a time
# sync, a lost project), none of which a faster poll would surface usefully
# sooner; it is small (a few dozen entries after a normal boot).
HEALTH_STATUS_REFRESH_INTERVAL = 900

# The config parameter that says whether the gateway's clock is currently
# unsynchronised (``GET /config/parameter/time_error`` returns a raw JSON
# boolean). The health log has no "time sync recovered" message, so this is
# what withdraws the time issue after a good sync (a later failed round
# withdraws it through the log instead — ``ISSUE_TIME_SYNC``'s clearing
# entry): ``sys_event_handler.js:127,132`` sets it
# ``false`` on every successful sync and ``true`` on every failed one
# (``api-server/dist/services/jung-configuration-service.js:171-185`` returns
# the stored value as-is).
TIME_ERROR_PARAMETER = "time_error"


@dataclass(frozen=True, slots=True)
class HealthEntry:
    """One entry of the gateway's health log."""

    level: str
    time: str
    description: str
    details: str


@dataclass(frozen=True, slots=True)
class HealthCondition:
    """A health-log condition the integration raises as a repair issue.

    ``key`` is both the issue's translation key and the prefix of its id.
    The condition holds while the newest entry matching ``raised_by`` is newer
    than the newest matching ``cleared_by`` (the log is newest first); a
    condition with no clearing message holds until the gateway restarts.
    """

    key: str
    severity: ir.IssueSeverity
    raised_by: frozenset[str]
    cleared_by: frozenset[str] = frozenset()


ISSUE_BLUETOOTH_FAILURE = "gateway_bluetooth_failure"
ISSUE_OUT_OF_SEQUENCE_NUMBERS = "gateway_out_of_sequence_numbers"
ISSUE_TIME_SYNC = "gateway_time_sync_failure"
ISSUE_PROJECT_MISSING = "gateway_project_missing"
ISSUE_PROJECT_INCOMPLETE = "gateway_project_incomplete"

# `ip_event_handler.js:528,585`: logged after every successful project import
# (upload from the app), after the flags below have been reset.
_NEW_PROJECT = "New Bluetooth Mesh Project"

HEALTH_CONDITIONS: tuple[HealthCondition, ...] = (
    HealthCondition(
        ISSUE_BLUETOOTH_FAILURE,
        ir.IssueSeverity.ERROR,
        frozenset(
            {
                # ncp_service.js:68 — the chip booted but node init failed.
                "JUNG HOME Gateway Bluetooth Chip start failure",
                # startup.js:151 — the Bluetooth adapter process did not start
                # (the message ends in a space; the error is in `details`).
                "was not able to start bluetooth adapter, details: ",
            }
        ),
    ),
    HealthCondition(
        ISSUE_OUT_OF_SEQUENCE_NUMBERS,
        ir.IssueSeverity.ERROR,
        # ncp_service.js:192 — the chip answered BT_MESH_LIMIT_REACHED (503):
        # the gateway can no longer send on the mesh until it is reset.
        frozenset({"out of sequence numbers"}),
    ),
    HealthCondition(
        ISSUE_TIME_SYNC,
        ir.IssueSeverity.WARNING,
        # sys_event_handler.js:154 — a failed sync more than 24 h after the
        # last successful one.
        frozenset({"JUNG HOME Gateway Time Sync Error"}),
        # Every failed sync first logs the generic `time error` flag entry
        # (sys_event_handler.js:132 sets `time_error` true, and
        # configuration_service.js:192-207 logs every `true` write of an
        # error-level parameter, `_` → ` `); only a failure > 24 h after the
        # last good sync then logs the entry above, later in the same
        # handler. So the newest of the two tells the rounds apart: a
        # > 24 h failure leaves `Time Sync Error` newest, a single missed NTP
        # round after a recovery leaves `time error` newest — which must not
        # resurrect the issue from the old entry the log never prunes. A
        # successful sync logs nothing: TIME_ERROR_PARAMETER withdraws the
        # issue while `Time Sync Error` is still the newest.
        frozenset({"time error"}),
    ),
    HealthCondition(
        ISSUE_PROJECT_MISSING,
        ir.IssueSeverity.WARNING,
        frozenset(
            {
                # project_file_service.js:156 — no project five minutes after
                # the gateway was provisioned.
                "JUNG HOME Project missing",
                # configuration_service.js:198-203 — `project_not_uploaded`
                # set true at node init (`String.replace("_", " ")` replaces
                # the first underscore only).
                "project not_uploaded",
            }
        ),
        frozenset({_NEW_PROJECT}),
    ),
    HealthCondition(
        ISSUE_PROJECT_INCOMPLETE,
        ir.IssueSeverity.WARNING,
        # project_file_service.js:202 — the stored project has no JUNG HOME
        # metadata (names, rooms, scenes); checked when the middleware starts.
        frozenset({"JUNG HOME project is incomplete"}),
        frozenset({_NEW_PROJECT}),
    ),
)


@dataclass(slots=True)
class HealthState:
    """The coordinator's view of one gateway's health log."""

    entry_id: str
    # The log as last read, newest first; None until a read has succeeded.
    entries: tuple[HealthEntry, ...] | None = None
    # The condition keys that read raised as repair issues.
    conditions: frozenset[str] = frozenset()
    # The periodic re-read's timer, and whether a read is running (reads are
    # not stacked).
    unsub: Callable[[], None] | None = None
    refresh_running: bool = False

    def issue_id(self, key: str) -> str:
        """Return the entry-scoped repair-issue id of a condition."""
        return f"{key}_{self.entry_id}"


def health_issue_ids(entry_id: str) -> list[str]:
    """Every health repair-issue id an entry can hold."""
    return [f"{condition.key}_{entry_id}" for condition in HEALTH_CONDITIONS]


def _text(raw: object) -> str:
    if isinstance(raw, str):
        return raw
    if raw is None:
        return ""
    try:
        return json.dumps(raw, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - JSON input is dumpable
        return str(raw)


def parse_health_status(raw: Any) -> tuple[HealthEntry, ...] | None:
    """Parse the ``/healthstatus/`` body; None if it is not a list.

    Entries that are not objects, or carry no string ``description``, are
    dropped; the order (newest first) is kept.
    """
    if not isinstance(raw, list):
        return None
    entries: list[HealthEntry] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        description = item.get("description")
        if not isinstance(description, str):
            continue
        entries.append(
            HealthEntry(
                level=_text(item.get("level")).upper(),
                time=_text(item.get("time")),
                description=description,
                details=_text(item.get("details")),
            )
        )
    return tuple(entries)


def active_health_conditions(
    entries: Sequence[HealthEntry],
) -> dict[str, HealthCondition]:
    """Return the conditions the log currently shows, keyed by issue key.

    Matching is on the exact description (trailing whitespace ignored): the
    messages are fixed English strings in the firmware, so a looser match
    could only catch something the table did not mean.
    """
    active: dict[str, HealthCondition] = {}
    for condition in HEALTH_CONDITIONS:
        for entry in entries:  # newest first
            description = entry.description.rstrip()
            if description in {d.rstrip() for d in condition.cleared_by}:
                break
            if description in {d.rstrip() for d in condition.raised_by}:
                active[condition.key] = condition
                break
    return active
