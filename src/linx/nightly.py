"""The nightly loop: keep every repo's backlog moving, with Jonny as the only merger.

    plan    LESTINXOR-AGENT (orchestrator) posts one `task.request` per repo for the first unchecked,
            unlocked backlog item. Never re-posts an item that is already open, in flight or done.
    work    An agent with an unattended command (e.g. Sebastian: `evolve --item {item} --pr`) claims its
            task, runs the command in the repo, and posts `task.result` (with any PR URL) or
            `task.failed` (with the output tail). Tasks without a command stay open for Jules/Claude.
    digest  A markdown morning report: what was planned, what landed, what failed, approvals and
            escalations waiting, memory conflicts.

Agents only ever open PRs. Nothing here merges, pushes to main or approves.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date as Date
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from linx import control
from linx.registry import Registry, System

KIND = "nightly"
MAX_ATTEMPTS = 3  # failures before an item is parked and escalated instead of retried
ITEM_RE = re.compile(r"^\s*- \[(?P<done>[ xX])\]\s*(?P<lock>🔒\s*)?\*\*(?P<bold>[^*]+)\*\*(?P<rest>.*)$")
ID_RE = re.compile(r"^(?P<id>[A-Za-z]+\d+[\w-]*|[A-Za-z]+-?\d+[\w-]*)[:.]?\s*(?P<title>.*)$")
PR_RE = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")

Runner = Callable[..., tuple[int, str]]


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    done: bool
    locked: bool
    in_review: bool  # the line says a PR is already open for it


def parse_backlog(text: str) -> list[Item]:
    items = []
    for line in text.splitlines():
        m = ITEM_RE.match(line)
        if not m:
            continue
        bold = m["bold"].strip()
        idm = ID_RE.match(bold)
        if not idm:
            continue
        title = (idm["title"] or m["rest"]).strip(" .:")
        rest = m["rest"]
        items.append(
            Item(
                idm["id"],
                title or idm["id"],
                m["done"] != " ",
                bool(m["lock"]),
                bool(re.search(r"\bPR open\b", bold + rest, re.I)),
            )
        )
    return items


def next_item(items: Sequence[Item], skip: set[str] = frozenset()) -> Item | None:  # type: ignore[assignment]
    for it in items:
        if not it.done and not it.locked and not it.in_review and it.id not in skip:
            return it
    return None


def task_id(day: str, system: str, item: str) -> str:
    return f"{KIND}/{day}/{system}/{item}"


def _nightly_tasks(view: Any) -> dict[str, dict[str, Any]]:
    """task_id -> request body, for every nightly task on the ledger."""
    return {
        e.task_id: dict(e.body)
        for e in view.entries
        if e.kind.value == "task.request" and e.body.get("flow") == KIND
    }


def _attempted(view: Any, system: str) -> set[str]:
    """Items for `system` that are open, claimed, approved or done (failed ones may be retried)."""
    out = set()
    for tid, body in _nightly_tasks(view).items():
        if body.get("system") == system and view.status(tid) not in ("failed", "rejected"):
            out.add(str(body.get("item")))
    return out


def failures(view: Any, system: str, item: str) -> list[str]:
    """Failure reasons for `system`/`item` since the owner last reset it with `linx nightly retry`."""
    mark = control.owner_value(view, f"retry:{system}/{item}")
    since = mark if isinstance(mark, int) else -1
    tasks = {
        tid for tid, b in _nightly_tasks(view).items() if b.get("system") == system and b.get("item") == item
    }
    return [
        str(e.body.get("reason", ""))
        for e in view.entries
        if e.kind.value == "task.failed" and e.task_id in tasks and e.seq > since
    ]


def parked(view: Any, system: str, items: Sequence[Item], max_attempts: int) -> set[str]:
    return {it.id for it in items if len(failures(view, system, it.id)) >= max_attempts}


IN_FLIGHT = ("open", "claimed", "awaiting_approval", "approved")


def _in_flight(view: Any, system: str) -> bool:
    """One nightly task per repo at a time: is one still open or being worked on?"""
    return any(
        body.get("system") == system and view.status(tid) in IN_FLIGHT
        for tid, body in _nightly_tasks(view).items()
    )


def plan(
    ledger: Any,
    registry: Registry,
    root: Path,
    *,
    day: str,
    sender: str = "lestinxor",
    read: Callable[[Path], str] | None = None,
    max_attempts: int = MAX_ATTEMPTS,
) -> list[str]:
    """Post tonight's tasks. Returns the task ids posted (none twice, however often it runs).

    An item that failed `max_attempts` times (since the owner's last `linx nightly retry`) is parked:
    it is skipped and escalated once, so a stuck item stops costing a run every night.
    """
    control.require_running(ledger)
    view = getattr(ledger, "ledger", ledger)
    read = read or (lambda p: p.read_text(encoding="utf-8"))
    escalated = {
        str(e.body.get("nightly_parked"))
        for e in view.entries
        if e.kind.value == "escalate" and "nightly_parked" in e.body
    }
    posted = []
    for s in registry:
        if not s.nightly_agent or _in_flight(view, s.name):
            continue
        backlog = root / s.repo / "docs" / "BACKLOG.md"
        try:
            items = parse_backlog(read(backlog))
        except OSError:
            continue
        stuck = parked(view, s.name, items, max_attempts)
        for item_id in sorted(stuck):
            ref = f"{s.name}/{item_id}"
            if ref not in escalated:
                last = failures(view, s.name, item_id)[-1:]
                ledger.post(
                    sender,
                    "escalate",
                    nightly_parked=ref,
                    reason=f"nightly {ref} failed {max_attempts}x and is parked "
                    f"(`linx nightly retry {s.name} {item_id}` to try again). Last: {last}",
                )
        item = next_item(items, _attempted(view, s.name) | stuck)
        if item is None:
            continue
        tid = task_id(day, s.name, item.id)
        if view.status(tid):
            continue
        earlier = failures(view, s.name, item.id)
        ledger.post(
            sender,
            "task.request",
            tid,
            title=f"{s.repo} {item.id}: {item.title}"[:200],
            flow=KIND,
            system=s.name,
            repo=s.slug(registry.owner),
            item=item.id,
            agent=s.nightly_agent,
            unattended=bool(s.nightly_command),
            attempt=len(earlier) + 1,
            previous_failures=earlier[-2:],
        )
        posted.append(tid)
    return posted


def retry(ledger: Any, system: str, item: str) -> None:
    """Owner resets an item's failure count so the nightly loop tries it again."""
    view = getattr(ledger, "ledger", ledger)
    head = view.entries[-1].seq if view.entries else -1
    ledger.post(control.OWNER, "memory.write", key=f"retry:{system}/{item}", value=head, scope=control.SCOPE)


def _run(
    argv: Sequence[str], cwd: Path, timeout: float, env: dict[str, str] | None = None
) -> tuple[int, str]:
    import os

    try:
        p = subprocess.run(
            list(argv),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, **(env or {})},
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout:.0f}s"
    except OSError as exc:
        return 127, str(exc)


def work(
    ledger: Any, registry: Registry, root: Path, agent: str, *, runner: Runner = _run, timeout: float = 3600.0
) -> str | None:
    """Claim and run one open nightly task for `agent`. Returns the task id handled, or None.

    Earlier failure reasons for the item reach the command as `{feedback}` and `LINX_FEEDBACK`.
    """
    control.require_running(ledger)
    view = getattr(ledger, "ledger", ledger)
    systems: dict[str, System] = {
        s.name: s for s in registry if s.nightly_agent == agent and s.nightly_command
    }
    for tid, body in sorted(_nightly_tasks(view).items()):
        s = systems.get(str(body.get("system")))
        if s is None or view.status(tid) not in ("open", "approved"):
            continue
        ledger.post(agent, "task.claim", tid)
        repo = root / s.repo
        feedback = " | ".join(str(r) for r in body.get("previous_failures") or [])
        argv = [
            a.replace("{item}", str(body["item"]))
            .replace("{repo}", str(repo))
            .replace("{feedback}", feedback)
            for a in s.nightly_command
        ]
        code, output = runner(argv, repo, timeout, {"LINX_FEEDBACK": feedback} if feedback else None)
        tail = output.strip()[-500:]
        if code == 0:
            pr = PR_RE.findall(output)
            summary = f"PR {pr[-1]}" if pr else (tail.splitlines()[-1] if tail else "done")
            ledger.post(agent, "task.result", tid, summary=summary[:500], pr=pr[-1] if pr else None)
        else:
            ledger.post(agent, "task.failed", tid, reason=f"exit {code}: {tail}"[:500])
        return tid
    return None


def _day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


def digest(ledger: Any, day: str, *, memory_report: Any = None) -> str:
    """Markdown morning report for the nightly tasks of `day`, plus what is waiting on Jonny."""
    view = getattr(ledger, "ledger", ledger)
    tasks = {t: b for t, b in _nightly_tasks(view).items() if t.startswith(f"{KIND}/{day}/")}
    results = {e.task_id: e for e in view.entries if e.kind.value in ("task.result", "task.failed")}
    icon = {
        "done": "✅",
        "failed": "❌",
        "claimed": "⏳",
        "open": "📋",
        "awaiting_approval": "🔐",
        "approved": "📋",
        "rejected": "🚫",
    }
    lines = [f"# LinX nightly digest: {day}", ""]
    state = control.halt_state(view)
    if state:
        lines += [
            f"**⛔ HALTED** by {control.OWNER}: {state.get('reason', '')} (`linx resume` to restart)",
            "",
        ]
    if not tasks:
        lines.append("No nightly tasks were planned.")
    for tid, body in sorted(tasks.items()):
        st = view.status(tid)
        who = view.claimant(tid) or body.get("agent", "?")
        attempt = int(body.get("attempt") or 1)
        tries = f", attempt {attempt}" if attempt > 1 else ""
        line = f"- {icon.get(st, '•')} **{body.get('system')}** `{body.get('item')}` {st} ({who}{tries})"
        e = results.get(tid)
        if e is not None:
            line += f": {e.body.get('summary') or e.body.get('reason')}"
        elif st == "open" and not body.get("unattended"):
            line += ": waiting for Jules/Claude to pick up"
        lines.append(line)
    pending = view.pending_approvals()
    lines += ["", f"## Waiting on you: {len(pending)} approval(s)"]
    lines += [f"- `{t}`" for t in pending]
    esc = [e for e in view.entries if e.kind.value == "escalate" and _day_of(e.ts) == day]
    lines += ["", f"## Escalations today: {len(esc)}"]
    lines += [f"- #{e.seq} {e.sender}: {e.body.get('reason')}" for e in esc[-10:]]
    if memory_report is not None:
        lines += [
            "",
            "## Shared memory",
            f"{memory_report.facts} facts, {len(memory_report.conflicts)} conflicts, "
            f"{len(memory_report.stale)} stale",
        ]
        lines += [f"- conflict `{c.ref}`" for c in memory_report.conflicts]
    return "\n".join(lines) + "\n"


def today() -> str:
    return Date.today().isoformat()
