"""Higher-level commands: one call that does several things.

repos    git state of every checkout (branch, dirty, ahead/behind, last commit); optional fetch, and
         a fast-forward-only pull for clean repos on their default branch
backlog  next item and counts for every repo's docs/BACKLOG.md, with parked nightly items
timeline readable ledger history, filterable by agent or task
find     search text across every checkout
assign   the owner posts a task for an agent
brief    the morning screen: halt, approvals, last night, memory conflicts, repos needing attention
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from linx import control, nightly
from linx.registry import Registry

Git = Callable[[Sequence[str], Path], tuple[int, str]]
SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "__pycache__",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "coverage",
    ".obsidian",
}
TEXT_EXT = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".cjs",
    ".mjs",
    ".md",
    ".json",
    ".toml",
    ".yml",
    ".yaml",
    ".sql",
    ".ps1",
    ".bat",
    ".txt",
    ".cfg",
    ".ini",
    ".html",
    ".css",
    ".sh",
}


def _git(args: Sequence[str], cwd: Path) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120, check=False)
        return p.returncode, (p.stdout or p.stderr).strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


# ── repos ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RepoState:
    system: str
    present: bool
    branch: str = ""
    dirty: int = 0  # changed/untracked files
    ahead: int | None = None  # None: no upstream
    behind: int | None = None
    last: str = ""
    action: str = ""  # what --fetch/--pull did

    @property
    def needs_attention(self) -> bool:
        return self.present and (self.dirty > 0 or bool(self.behind) or bool(self.ahead))


def repo_state(
    system: str, path: Path, *, fetch: bool = False, pull: bool = False, git: Git = _git
) -> RepoState:
    if not (path / ".git").exists():
        return RepoState(system, False)
    action = ""
    if fetch or pull:
        code, out = git(["fetch", "--quiet"], path)
        action = "fetched" if code == 0 else f"fetch failed: {out[:80]}"
    _, branch = git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    _, status = git(["status", "--porcelain"], path)
    dirty = len([line for line in status.splitlines() if line.strip()])
    ahead = behind = None
    code, counts = git(["rev-list", "--left-right", "--count", "@{u}...HEAD"], path)
    if code == 0 and len(counts.split()) == 2:
        behind, ahead = (int(x) for x in counts.split())
    if pull and behind:
        code, default = git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path)
        default_branch = default.removeprefix("origin/") if code == 0 else ""
        if dirty:
            action = "not pulled: uncommitted changes"
        elif branch != default_branch:
            action = f"not pulled: on {branch}, not {default_branch or 'the default branch'}"
        else:
            code, out = git(["merge", "--ff-only", "@{u}"], path)
            action = "pulled (fast-forward)" if code == 0 else f"not pulled: {out[:80]}"
            if code == 0:
                behind = 0
    _, last = git(["log", "-1", "--format=%h %cr: %s"], path)
    return RepoState(system, True, branch, dirty, ahead, behind, last[:90], action)


def repos(
    registry: Registry, root: Path, *, fetch: bool = False, pull: bool = False, git: Git = _git
) -> list[RepoState]:
    return [repo_state(s.name, root / s.repo, fetch=fetch, pull=pull, git=git) for s in registry]


def render_repos(states: Sequence[RepoState]) -> str:
    lines = [f"{'system':<17} {'branch':<22} {'dirty':>5} {'ahead':>5} {'behind':>6}  last commit"]
    for r in states:
        if not r.present:
            lines.append(f"{r.system:<17} MISSING")
            continue
        a = "-" if r.ahead is None else str(r.ahead)
        b = "-" if r.behind is None else str(r.behind)
        flag = " !" if r.needs_attention else ""
        lines.append(f"{r.system:<17} {r.branch[:22]:<22} {r.dirty:>5} {a:>5} {b:>6}  {r.last}{flag}")
        if r.action:
            lines.append(f"{'':17} -> {r.action}")
    return "\n".join(lines)


# ── backlog ──────────────────────────────────────────────────────────────────


def backlog(registry: Registry, root: Path, ledger: Any = None, only: str | None = None) -> str:
    view = getattr(ledger, "ledger", ledger)
    lines = []
    for s in registry:
        if only and s.name != only:
            continue
        path = root / s.repo / "docs" / "BACKLOG.md"
        if not path.is_file():
            continue
        items = nightly.parse_backlog(path.read_text(encoding="utf-8"))
        done = sum(i.done for i in items)
        locked = sum(not i.done and i.locked for i in items)
        review = sum(not i.done and i.in_review for i in items)
        open_ = sum(not i.done and not i.locked and not i.in_review for i in items)
        stuck = nightly.parked(view, s.name, items, nightly.MAX_ATTEMPTS) if view is not None else set()
        nxt = nightly.next_item(items, stuck)
        lines.append(f"{s.name}: {open_} open, {locked} locked, {review} in review, {done} done")
        lines.append(f"  next: {nxt.id} {nxt.title}" if nxt else "  next: (nothing unlocked)")
        if stuck:
            lines.append(f"  parked after {nightly.MAX_ATTEMPTS} failures: {', '.join(sorted(stuck))}")
    return "\n".join(lines) if lines else "no backlogs found"


# ── timeline ─────────────────────────────────────────────────────────────────


def _summary(e: Any) -> str:
    b = e.body
    for key in ("title", "summary", "reason", "action"):
        if b.get(key):
            return str(b[key])
    if e.kind.value == "memory.write":
        return f"{b.get('scope', 'shared')}/{b.get('key')} = {str(b.get('value'))[:60]}"
    if e.kind.value == "approval.decision":
        return "approved" if b.get("approved") else "rejected"
    return ""


def timeline(ledger: Any, *, agent: str | None = None, task: str | None = None, last: int = 20) -> str:
    view = getattr(ledger, "ledger", ledger)
    rows = [e for e in view.entries if (not agent or e.sender == agent) and (not task or task in e.task_id)]
    out = []
    for e in rows[-last:]:
        tid = f" [{e.task_id}]" if e.task_id else ""
        out.append(f"#{e.seq:<5} {e.sender:<15} {e.kind.value:<17}{tid} {_summary(e)}"[:200])
    return "\n".join(out) if out else "no matching entries"


# ── find ─────────────────────────────────────────────────────────────────────


def _files(base: Path) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in TEXT_EXT:
                yield p


def find(
    registry: Registry, root: Path, pattern: str, *, only: str | None = None, limit: int = 50
) -> list[str]:
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = re.compile(re.escape(pattern), re.I)
    hits: list[str] = []
    for s in registry:
        if only and s.name != only:
            continue
        base = root / s.repo
        if not base.is_dir():
            continue
        for path in sorted(_files(base)):
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{s.repo}/{path.relative_to(base).as_posix()}:{n}: {line.strip()[:160]}")
                    if len(hits) >= limit:
                        return hits
    return hits


# ── assign ───────────────────────────────────────────────────────────────────


def assign(ledger: Any, title: str, agent: str, *, approval: bool = False) -> str:
    """The owner posts a task for `agent`. Returns its task id."""
    view = getattr(ledger, "ledger", ledger)
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40] or "task"
    head = view.entries[-1].digest if view.entries else ""
    tid = f"{control.OWNER}/{slug}-{hashlib.sha256((head + title).encode()).hexdigest()[:6]}"
    ledger.post(
        control.OWNER, "task.request", tid, title=title[:200], agent=agent, requires_approval=approval
    )
    return tid


# ── brief ────────────────────────────────────────────────────────────────────


def brief(registry: Registry, root: Path, ledger: Any, *, day: str, git: Git = _git) -> str:
    from linx import memory

    view = getattr(ledger, "ledger", ledger)
    lines = [f"LinX brief: {day}", ""]
    halt = control.halt_state(view)
    if halt:
        lines += [f"⛔ HALTED: {halt.get('reason', '')} (linx resume)", ""]
    verified = view.verify()
    lines.append(f"ledger: {len(view.entries)} entries, {'verified' if verified else 'CHAIN BROKEN'}")
    waiting = control.pending(view)
    lines.append(f"waiting on you: {len(waiting)}")
    lines += [f"  {t}  {title}" for t, title, _ in waiting[:5]]
    tasks = {t: b for t, b in nightly._nightly_tasks(view).items() if t.startswith(f"nightly/{day}/")}
    counts: dict[str, int] = {}
    for t in tasks:
        st = view.status(t)
        counts[st] = counts.get(st, 0) + 1
    last_night = ", ".join(f"{v} {k}" for k, v in sorted(counts.items())) or "nothing planned"
    lines.append(f"last night: {last_night}")
    report = memory.consolidate(view.entries)
    lines.append(
        f"memory: {report.facts} facts, {len(report.conflicts)} conflicts, {len(report.stale)} stale"
    )
    lines += [f"  conflict {c.ref}" for c in report.conflicts[:3]]
    states = repos(registry, root, git=git)
    attention = [r for r in states if r.needs_attention]
    missing = [r.system for r in states if not r.present]
    lines.append(
        f"repos: {len(states) - len(missing)} present, {len(attention)} need attention"
        + (f", missing: {', '.join(missing)}" if missing else "")
    )
    lines += [
        f"  {r.system}: {r.dirty} dirty, {r.behind or 0} behind, {r.ahead or 0} ahead" for r in attention
    ]
    return "\n".join(lines)
