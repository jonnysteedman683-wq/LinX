"""LinX active memory (stage 1): one shared memory on the UniSync ledger, kept active by consolidation.

The ledger is the single record of truth. Every fact is a `memory.write` envelope (scope, key,
value) posted by a registered agent, so every value has an author, a position and a hash chain.
This module reads that history and keeps it useful:

* `index`        full history per (scope, key): who wrote what, in ledger order.
* `consolidate`  a deterministic report over the index:
    - conflicts:  two or more agents wrote different values for one key in its recent history;
    - stale:      a key's last write is `stale_after` or more entries behind the ledger head;
    - duplicates: different keys in one scope holding the identical value (merge candidates);
    - links:      a value that names another key (`scope/key` or a bare key in the same scope).
* `cards`        Obsidian cards in HORIZONTAL-VAULT's format, one per key plus an index card. They
                 are written to a folder you choose; HORIZONTAL stays the only writer of the vault.
* `post`         makes it active: an agent posts the consolidation summary back as memory
                 (scope `linx/memory`) and one `escalate` per conflict it has not escalated before.

Reading never writes. Writing always goes through `Ledger.post`, so roles and authority still apply.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SUMMARY_SCOPE = "linx/memory"
CONFLICT_WINDOW = 5  # recent writes per key checked for disagreement


@dataclass(frozen=True)
class Write:
    seq: int
    sender: str
    value: Any
    ts: float


@dataclass
class Fact:
    scope: str
    key: str
    history: list[Write] = field(default_factory=list)

    @property
    def ref(self) -> str:
        return f"{self.scope}/{self.key}"

    @property
    def current(self) -> Write:
        return self.history[-1]

    @property
    def writers(self) -> list[str]:
        return sorted({w.sender for w in self.history})


@dataclass(frozen=True)
class Conflict:
    ref: str
    values: dict[str, Any]  # sender -> their latest value in the window


@dataclass(frozen=True)
class Report:
    head: int
    facts: int
    conflicts: list[Conflict]
    stale: list[str]
    duplicates: list[list[str]]
    links: dict[str, list[str]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def digest(self) -> str:
        """Content digest, excluding `head`: posting the summary moves the head but changes no finding."""
        body = {k: v for k, v in self.to_dict().items() if k != "head"}
        return hashlib.sha256(_canon(body)).hexdigest()


def _canon(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def index(entries: Iterable[Any]) -> dict[str, Fact]:
    """Every memory.write in ledger order, grouped by `scope/key`."""
    facts: dict[str, Fact] = {}
    for e in entries:
        if e.kind.value != "memory.write":
            continue
        scope = str(e.body.get("scope", "shared"))
        key = str(e.body["key"])
        fact = facts.setdefault(f"{scope}/{key}", Fact(scope, key))
        fact.history.append(Write(e.seq, e.sender, e.body["value"], float(e.ts)))
    return facts


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def consolidate(
    entries: Iterable[Any], stale_after: int = 500, skip_scopes: tuple[str, ...] = (SUMMARY_SCOPE,)
) -> Report:
    entries = list(entries)
    head = entries[-1].seq if entries else -1
    facts = {r: f for r, f in index(entries).items() if f.scope not in skip_scopes}

    conflicts = []
    for ref, fact in sorted(facts.items()):
        latest: dict[str, Any] = {}
        for w in fact.history[-CONFLICT_WINDOW:]:
            latest[w.sender] = w.value
        if len(latest) > 1 and len({_canon(v) for v in latest.values()}) > 1:
            conflicts.append(Conflict(ref, dict(sorted(latest.items()))))

    stale = sorted(r for r, f in facts.items() if head - f.current.seq >= stale_after)

    groups: dict[tuple[str, bytes], list[str]] = {}
    for ref, fact in facts.items():
        groups.setdefault((fact.scope, _canon(fact.current.value)), []).append(ref)
    duplicates = sorted(sorted(refs) for refs in groups.values() if len(refs) > 1)

    links: dict[str, list[str]] = {}
    for ref, fact in sorted(facts.items()):
        found = set()
        for text in _strings(fact.current.value):
            for other, of in facts.items():
                if other == ref:
                    continue
                pattern = rf"(?<![\w/]){re.escape(other)}(?![\w/])"
                bare = rf"(?<![\w/]){re.escape(of.key)}(?![\w/])"
                if re.search(pattern, text) or (of.scope == fact.scope and re.search(bare, text)):
                    found.add(other)
        if found:
            links[ref] = sorted(found)
    return Report(head, len(facts), conflicts, stale, duplicates, links)


# ── vault cards ──────────────────────────────────────────────────────────────


def _slug(ref: str) -> str:
    return "mem_" + re.sub(r"[^A-Za-z0-9]+", "_", ref).strip("_").lower()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


def _value_md(value: Any) -> str:
    if isinstance(value, str) and "\n" not in value and len(value) < 200:
        return value
    return "```json\n" + json.dumps(value, indent=2, sort_keys=True, default=str) + "\n```"


def cards(entries: Iterable[Any], report: Report | None = None) -> dict[str, str]:
    """`{filename: markdown}`: one card per fact plus `000_UniSync_Memory_MOC.md`."""
    entries = list(entries)
    report = report or consolidate(entries)
    facts = {r: f for r, f in index(entries).items() if f.scope != SUMMARY_SCOPE}
    conflicted = {c.ref for c in report.conflicts}
    stale = set(report.stale)
    dupes = {r: [o for o in g if o != r] for g in report.duplicates for r in g}
    out: dict[str, str] = {}
    for ref, f in sorted(facts.items()):
        tags = [f"unisync/{f.scope}", *(f"agent/{w}" for w in f.writers)]
        if ref in conflicted:
            tags.append("memory/conflict")
        if ref in stale:
            tags.append("memory/stale")
        flags = [x for x, on in (("⚠️ conflict", ref in conflicted), ("💤 stale", ref in stale)) if on]
        lines = [
            "---",
            f"id: {_slug(ref)}",
            f'title: "Memory: {ref}"',
            f'domain: "{f.scope}"',
            "type: unisync_memory",
            "tags:",
            *(f'  - "{t}"' for t in tags),
            'up: "[[000_UniSync_Memory_MOC]]"',
            f'created_at: "{_iso(f.history[0].ts)}"',
            f"ledger_seq: {f.current.seq}",
            "---",
            "",
            f"# 🧠 {ref}" + (f"  ({', '.join(flags)})" if flags else ""),
            "",
            "### Current value",
            f"Set by **{f.current.sender}** at ledger #{f.current.seq}.",
            "",
            _value_md(f.current.value),
            "",
        ]
        if ref in conflicted:
            c = next(c for c in report.conflicts if c.ref == ref)
            lines += [
                "### ⚠️ Conflicting values",
                *(f"- **{s}**: `{json.dumps(v, default=str)}`" for s, v in c.values.items()),
                "",
            ]
        related = [*report.links.get(ref, []), *dupes.get(ref, [])]
        if related:
            lines += [
                "### 🔗 Connected Nodes",
                *(f"- [[{_slug(r)}]] `{r}`" for r in sorted(set(related))),
                "",
            ]
        lines += ["### History", *(f"- #{w.seq} {w.sender}" for w in reversed(f.history)), ""]
        out[_slug(ref) + ".md"] = "\n".join(lines)

    moc = [
        "---",
        "id: 000_UniSync_Memory_MOC",
        'title: "UniSync Memory"',
        "type: moc",
        "---",
        "",
        "# 🧠 UniSync Memory",
        "",
        f"{report.facts} facts at ledger #{report.head}: {len(report.conflicts)} conflicts, "
        f"{len(report.stale)} stale, {len(report.duplicates)} duplicate groups.",
        "",
    ]
    for scope in sorted({f.scope for f in facts.values()}):
        moc += [
            f"## {scope}",
            *(f"- [[{_slug(r)}]] `{r}`" for r, f in sorted(facts.items()) if f.scope == scope),
            "",
        ]
    out["000_UniSync_Memory_MOC.md"] = "\n".join(moc)
    return out


def write_cards(entries: Iterable[Any], out_dir: str | Path) -> list[Path]:
    """Write cards to `out_dir` (created if missing). Refuses a folder that looks like a live vault."""
    out = Path(out_dir)
    if (out / ".obsidian").exists() or (out / "00_Gateway").exists():
        raise ValueError(
            f"{out} looks like a vault root; write to a staging folder and let HORIZONTAL import it"
        )
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, text in cards(entries).items():
        path = out / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    return written


# ── active: post back ────────────────────────────────────────────────────────


def post(ledger: Any, sender: str, report: Report) -> int:
    """Post the summary (scope `linx/memory`, key `consolidation`) and escalate new conflicts.

    Idempotent: nothing is posted when the report digest matches the last summary, and a conflict
    is escalated once per distinct set of values. Returns how many envelopes were posted.
    """
    view = getattr(ledger, "ledger", ledger)  # SupabaseLedger / FileLedger wrap a Ledger
    previous = view.memory(SUMMARY_SCOPE).get("consolidation")
    digest = report.digest()
    posted = 0
    escalated = {
        str(e.body.get("memory_conflict_id"))
        for e in view.entries
        if e.kind.value == "escalate" and "memory_conflict_id" in e.body
    }
    for c in report.conflicts:
        cid = hashlib.sha256(_canon([c.ref, c.values])).hexdigest()[:16]
        if cid in escalated:
            continue
        who = ", ".join(c.values)
        ledger.post(
            sender,
            "escalate",
            reason=f"memory conflict on {c.ref}: {who} disagree",
            memory_ref=c.ref,
            memory_conflict_id=cid,
        )
        posted += 1
    if isinstance(previous, dict) and previous.get("digest") == digest:
        return posted
    summary = {
        "digest": digest,
        "head": report.head,
        "facts": report.facts,
        "conflicts": [c.ref for c in report.conflicts],
        "stale": report.stale,
        "duplicates": report.duplicates,
        "links": len(report.links),
    }
    ledger.post(sender, "memory.write", key="consolidation", value=summary, scope=SUMMARY_SCOPE)
    return posted + 1


# ── the active loop ──────────────────────────────────────────────────────────


def is_transient(exc: BaseException) -> bool:
    """Network trouble worth retrying; anything else (protocol, auth, config) is not."""
    import urllib.error

    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or exc.code >= 500
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))


def run(
    ledger: Any,
    sender: str,
    *,
    stale_after: int = 500,
    every: float | None = None,
    max_rounds: int | None = None,
    max_backoff: float = 600.0,
    sleep: Any = None,
    log: Any = None,
) -> int:
    """Consolidate once, or every `every` seconds. Transient network errors back off and retry;
    anything else is raised. Returns the total envelopes posted."""
    import logging
    import time

    sleep = sleep or time.sleep
    log = log or logging.getLogger("linx.memory")
    rounds = posted = 0
    delay = every or 0.0
    while True:
        rounds += 1
        try:
            sync = getattr(ledger, "sync", None)
            if callable(sync):
                sync()
            view = getattr(ledger, "ledger", ledger)
            report = consolidate(view.entries, stale_after=stale_after)
            n = post(ledger, sender, report)
            posted += n
            delay = every or 0.0
            log.info(
                "%d facts, %d conflicts, %d stale; posted %d",
                report.facts,
                len(report.conflicts),
                len(report.stale),
                n,
            )
        except Exception as exc:
            if not every or not is_transient(exc):
                raise
            delay = min(max(delay * 2, every), max_backoff)
            log.warning("transient error (%s); retrying in %.0fs", exc, delay)
        if not every or (max_rounds is not None and rounds >= max_rounds):
            return posted
        sleep(delay)
