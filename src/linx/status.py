"""`linx status`: one view of the fleet. Checkouts (doctor) joined with the UniSync ledger:
per-system task load and last activity, pending approvals, recent escalations, and the latest
Vortex task states mirrored by the relay."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from linx import relay
from linx.doctor import Report, doctor
from linx.registry import Registry


@dataclass(frozen=True)
class SystemStatus:
    name: str
    layer: str
    checkout: str  # short commit, "MISSING", or "present"
    agent: bool  # registered with a UniSync role in LinX
    claimed: int = 0  # tasks this agent currently holds
    requested_open: int = 0  # tasks it requested that are still open / approved / claimed
    awaiting_approval: int = 0  # tasks it requested that wait on the owner
    done: int = 0
    failed: int = 0
    last_seq: int | None = None  # last ledger entry it posted


@dataclass(frozen=True)
class Approval:
    task_id: str
    title: str
    requested_by: str


@dataclass(frozen=True)
class Escalation:
    seq: int
    sender: str
    reason: str


@dataclass
class FleetStatus:
    systems: list[SystemStatus]
    ledger: str  # "none", a file path, or "supabase"
    entries: int = 0
    verified: bool | None = None
    approvals: list[Approval] = field(default_factory=list)
    escalations: list[Escalation] = field(default_factory=list)
    vortex: dict[str, int] = field(default_factory=dict)  # Vortex task status -> count
    halted: str | None = None  # the owner's halt reason while LinX is halted

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _checkout(report: Report) -> str:
    if not report.present:
        return "MISSING"
    return report.head or "present"


def build(
    registry: Registry, root: Path, ledger: Any = None, source: str = "none", escalation_limit: int = 5
) -> FleetStatus:
    """`ledger` is a lestinxor `Ledger` (anything with `entries`, `tasks`, `status`, `claimant`, `memory`)."""
    reports = {r.system: r for r in doctor(registry, root)}
    if ledger is None:
        return FleetStatus(
            [SystemStatus(s.name, s.layer, _checkout(reports[s.name]), s.role is not None) for s in registry],
            source,
        )

    entries = list(ledger.entries)
    requester = {e.task_id: e.sender for e in entries if e.kind.value == "task.request"}
    titles = {e.task_id: str(e.body.get("title", "")) for e in entries if e.kind.value == "task.request"}
    last_seq: dict[str, int] = {e.sender: e.seq for e in entries}
    state = {t: ledger.status(t) for t in ledger.tasks()}

    systems = []
    for s in registry:
        mine = [t for t, who in requester.items() if who == s.name]
        held = [t for t, st in state.items() if st == "claimed" and ledger.claimant(t) == s.name]
        finished = Counter(state[t] for t in state if ledger.claimant(t) == s.name)
        systems.append(
            SystemStatus(
                s.name,
                s.layer,
                _checkout(reports[s.name]),
                s.role is not None,
                claimed=len(held),
                requested_open=sum(state[t] in ("open", "approved", "claimed") for t in mine),
                awaiting_approval=sum(state[t] == "awaiting_approval" for t in mine),
                done=finished["done"],
                failed=finished["failed"],
                last_seq=last_seq.get(s.name),
            )
        )

    approvals = [
        Approval(t, titles.get(t, ""), requester.get(t, ""))
        for t, st in state.items()
        if st == "awaiting_approval"
    ]
    escalations = [
        Escalation(e.seq, e.sender, str(e.body.get("reason", "")))
        for e in entries
        if e.kind.value == "escalate"
    ][-escalation_limit:]
    vortex = dict(sorted(Counter(str(v.get("status", "?")) for v in relay.tasks(ledger).values()).items()))
    from linx import control

    halt = control.halt_state(ledger)
    halted = str(halt.get("reason", "")) if halt else None
    return FleetStatus(systems, source, len(entries), ledger.verify(), approvals, escalations, vortex, halted)


def render(status: FleetStatus) -> str:
    lines = [
        f"{'system':<17} {'layer':<14} {'checkout':<9} {'held':>4} {'open':>4} {'appr':>4} "
        f"{'done':>4} {'fail':>4}  last"
    ]
    for s in status.systems:
        if status.ledger == "none" or not s.agent:
            nums = f"{'-':>4} {'-':>4} {'-':>4} {'-':>4} {'-':>4}  -"
        else:
            last = "-" if s.last_seq is None else f"#{s.last_seq}"
            nums = (
                f"{s.claimed:>4} {s.requested_open:>4} {s.awaiting_approval:>4} "
                f"{s.done:>4} {s.failed:>4}  {last}"
            )
        lines.append(f"{s.name:<17} {s.layer:<14} {s.checkout:<9} {nums}")
    if status.ledger == "none":
        lines.append("\nledger: none (pass --ledger FILE or set UNISYNC_SUPABASE_URL/_KEY)")
        return "\n".join(lines)
    if status.halted is not None:
        lines.insert(0, f"⛔ HALTED by jonny: {status.halted}  (linx resume to lift)\n")
    ok = "verified" if status.verified else "CHAIN BROKEN"
    lines.append(f"\nledger: {status.ledger}, {status.entries} entries, {ok}")
    lines.append(f"pending approvals: {len(status.approvals)}")
    lines += [f"  {a.task_id}  {a.title}  (from {a.requested_by})" for a in status.approvals]
    if status.vortex:
        lines.append("vortex tasks: " + ", ".join(f"{k} {v}" for k, v in status.vortex.items()))
    if status.escalations:
        lines.append("recent escalations:")
        lines += [f"  #{e.seq} {e.sender}: {e.reason}" for e in status.escalations]
    return "\n".join(lines)
