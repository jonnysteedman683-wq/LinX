"""Owner controls on the UniSync ledger: the kill switch and approvals.

* `halt` / `resume`: the owner writes `linx/control/halt`. Only the owner's writes count, so no agent
  can halt the fleet or lift a halt. While halted, `linx nightly` refuses to plan or work and the
  memory loop skips its rounds; the relay keeps mirroring (it only observes). Pausing Vortex agents
  themselves stays a Vortex operator action (`vortex_operator_pause_agent`).
* `decide`: an `approval.decision` posted as the owner. The ledger itself refuses it from anyone else
  and for tasks that are not awaiting approval.
"""

from __future__ import annotations

from typing import Any

OWNER = "jonny"
SCOPE = "linx/control"
HALT_KEY = "halt"


class Halted(RuntimeError):
    """Raised by loops that must not run while the owner has halted LinX."""


def _view(ledger: Any) -> Any:
    return getattr(ledger, "ledger", ledger)


def owner_value(ledger: Any, key: str) -> Any:
    """The latest value the owner wrote under `linx/control/<key>`, ignoring every other sender."""
    value = None
    for e in _view(ledger).entries:
        if (
            e.kind.value == "memory.write"
            and e.sender == OWNER
            and e.body.get("scope") == SCOPE
            and e.body.get("key") == key
        ):
            value = e.body.get("value")
    return value


def halt_state(ledger: Any) -> dict[str, Any] | None:
    """The active halt (`{"halted": True, "reason": ...}`) or None."""
    value = owner_value(ledger, HALT_KEY)
    return value if isinstance(value, dict) and value.get("halted") else None


def require_running(ledger: Any) -> None:
    state = halt_state(ledger)
    if state:
        raise Halted(
            f"LinX is halted by {OWNER}: {state.get('reason', 'no reason given')} (run `linx resume`)"
        )


def halt(ledger: Any, reason: str) -> None:
    ledger.post(OWNER, "memory.write", key=HALT_KEY, value={"halted": True, "reason": reason}, scope=SCOPE)
    ledger.post(OWNER, "escalate", reason=f"LinX halted: {reason}", control="halt")


def resume(ledger: Any, reason: str = "") -> None:
    ledger.post(OWNER, "memory.write", key=HALT_KEY, value={"halted": False, "reason": reason}, scope=SCOPE)


def decide(ledger: Any, task_id: str, approved: bool, note: str = "") -> Any:
    body: dict[str, Any] = {"approved": approved}
    if note:
        body["note" if approved else "reason"] = note
    return ledger.post(OWNER, "approval.decision", task_id, **body)


def pending(ledger: Any) -> list[tuple[str, str, str]]:
    """(task_id, title, requested_by) for every task awaiting the owner."""
    view = _view(ledger)
    req = {e.task_id: e for e in view.entries if e.kind.value == "task.request"}
    out = []
    for tid in view.pending_approvals():
        e = req.get(tid)
        out.append((tid, str(e.body.get("title", "")) if e else "", e.sender if e else ""))
    return out
