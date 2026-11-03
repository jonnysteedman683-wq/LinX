"""Vortex -> UniSync relay (X3).

Mirrors Vortex's fleet-wide task event feed (`vortex_operator_events`) onto the UniSync ledger,
posting as the `vortex` watcher. Read-only on Vortex. Vortex tasks are never re-created as UniSync
tasks: a watcher can't dispatch, and work must not be routed twice. Instead:

* every event becomes a `memory.write` in scope `vortex`, key `task/<task_id>`, so the ledger holds
  the last known state of each Vortex task (last write wins);
* a `blocked` event also posts an `escalate`, so watchers and Jonny see it.

The cursor (last relayed `event_id`) is read back from the ledger itself, so relaying the same feed
twice appends nothing. Every event is validated before anything is posted.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Protocol

SCOPE = "vortex"
SENDER = "vortex"

# Vortex task status after each event type (Vortex `tasks.status` values).
STATUS_AFTER: Mapping[str, str] = {
    "created": "open",
    "claimed": "in_progress",
    "in_progress": "in_progress",
    "blocked": "blocked",
    "completed": "completed",
    "cancelled": "cancelled",
    "approved": "open",
    "rejected": "cancelled",
}


class RelayError(ValueError):
    """A Vortex event is malformed. Nothing is posted when this is raised."""


class LedgerLike(Protocol):
    def post(self, sender: str, kind: str, task_id: str = "", **body: Any) -> Any: ...

    def memory(self, scope: str = "shared") -> dict[str, Any]: ...


def cursor(ledger: LedgerLike) -> int:
    """The highest Vortex event_id already on the ledger (0 if none)."""
    ids = [v.get("event_id", 0) for v in ledger.memory(SCOPE).values() if isinstance(v, dict)]
    return max([i for i in ids if isinstance(i, int)], default=0)


def _check(event: Mapping[str, Any]) -> None:
    eid = event.get("event_id")
    if isinstance(eid, bool) or not isinstance(eid, int) or eid < 1:
        raise RelayError(f"bad event_id: {eid!r}")
    for key in ("task_id", "actor_id"):
        if not isinstance(event.get(key), str) or not event[key]:
            raise RelayError(f"event {eid}: missing {key}")
    if not isinstance(event.get("event_type"), str) or event["event_type"] not in STATUS_AFTER:
        raise RelayError(f"event {eid}: unknown event_type {event.get('event_type')!r}")
    if not isinstance(event.get("details", {}), Mapping):
        raise RelayError(f"event {eid}: details must be an object")


def _state(event: Mapping[str, Any]) -> dict[str, Any]:
    details = dict(event.get("details") or {})
    kind = event["event_type"]
    status = STATUS_AFTER[kind]
    if kind == "created" and details.get("requires_approval"):
        status = "pending_approval"
    return {
        "event_id": event["event_id"],
        "event": kind,
        "actor": event["actor_id"],
        "status": status,
        "goal": event.get("task_goal", ""),
        "at": event.get("created_at", ""),
        "details": details,
    }


def _escalated(ledger: Any) -> set[int]:
    """Vortex event ids that already have an escalation on the ledger."""
    entries = getattr(ledger, "entries", None)
    if entries is None:
        entries = getattr(getattr(ledger, "ledger", None), "entries", [])
    return {
        e.body.get("vortex_event")
        for e in entries
        if e.sender == SENDER and getattr(e.kind, "value", e.kind) == "escalate"
    }


def relay(ledger: LedgerLike, events: Iterable[Mapping[str, Any]]) -> int:
    """Post every event newer than the ledger's cursor, in event_id order. Returns how many were relayed."""
    batch = list(events)
    for event in batch:
        _check(event)
    ids = [e["event_id"] for e in batch]
    if len(ids) != len(set(ids)):
        raise RelayError("duplicate event_id in feed")
    start = cursor(ledger)
    fresh = sorted((e for e in batch if e["event_id"] > start), key=lambda e: e["event_id"])
    posted = 0
    for event in fresh:
        if cursor(ledger) >= event["event_id"]:
            continue  # another relay posted it after our first read (a shared ledger re-syncs on post)
        state = _state(event)
        # Escalate first: the memory write advances the cursor, so a crash between the two posts must
        # never leave a blocked task without its escalation. A retry skips an escalation already posted.
        if event["event_type"] == "blocked" and event["event_id"] not in _escalated(ledger):
            reason = state["details"].get("blocked_reason") or "no reason given"
            ledger.post(
                SENDER,
                "escalate",
                reason=f"Vortex task {event['task_id']} blocked: {reason}",
                vortex_task=event["task_id"],
                vortex_event=event["event_id"],
                actor=event["actor_id"],
            )
        ledger.post(SENDER, "memory.write", key=f"task/{event['task_id']}", value=state, scope=SCOPE)
        posted += 1
    return posted


def tasks(ledger: LedgerLike) -> dict[str, dict[str, Any]]:
    """Last known state of each relayed Vortex task, keyed by Vortex task_id."""
    return {k.removeprefix("task/"): v for k, v in ledger.memory(SCOPE).items() if k.startswith("task/")}
