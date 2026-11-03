"""Chaos and fuzz tests (hardening 7).

Seeded, deterministic, stdlib only: every failure prints the seed and the input that broke it, so it
can be replayed. `LINX_FUZZ_ITERS` raises the iteration count (default 200 per test) for a deeper run.

Properties:
* Ledger replay itself is fuzzed in Lestinxor (tests/test_unisync_fuzz.py, L10).
* Backups and anchors: damaged files raise BackupError only.
* Vortex feed: random SSE text and random events raise RelayError/ValueError only, and a bad batch
  posts nothing.
* linx ask: no English input and no model reply can produce a command outside the allowlist.
* Repo expert: no query (FTS5 operators, quotes, unicode) makes search crash.
* Chaos: concurrent Supabase writers with injected conflicts, 503s and lost responses never fork the
  ledger; the live relay under random network faults relays every event exactly once, in order.
"""

from __future__ import annotations

import io
import json
import os
import random
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger, ProtocolError

from linx import ask, backup, expert, live, load, relay
from linx.cli import _parser

ITERS = int(os.environ.get("LINX_FUZZ_ITERS", "200"))
JUNK: list[Any] = [None, 0, -1, 2**70, 1.5, float("inf"), True, "", "x", [], [1], {}, {"a": 1}, "‮", "\x00"]


def rng(seed: int) -> random.Random:
    return random.Random(seed)


def real_ledger(n: int = 12) -> Ledger:
    lg = Ledger(clock=ManualClock())
    lg.post("jonny", Kind.TASK_REQUEST, "T1", title="audit", assignee="o0")
    lg.post("o0", Kind.TASK_CLAIM, "T1")
    for i in range(n):
        lg.post("sebastian", Kind.MEMORY_WRITE, key=f"k{i % 4}", value={"n": i}, scope="shared")
    lg.post("o0", Kind.TASK_RESULT, "T1", summary="done")
    return lg


def mutate(r: random.Random, lines: list[str]) -> list[str]:
    out = list(lines)
    op = r.randrange(9)
    i = r.randrange(len(out))
    if op == 0:
        del out[i]
    elif op == 1:
        j = r.randrange(len(out))
        out[i], out[j] = out[j], out[i]
    elif op == 2:
        out.insert(r.randrange(len(out) + 1), out[i])
    elif op == 3:
        cut = r.randrange(len(out[i]) + 1)
        out[i] = out[i][:cut]
    elif op == 4:
        s = out[i]
        k = r.randrange(len(s))
        out[i] = s[:k] + chr(r.randrange(32, 0x2FF)) + s[k + 1 :]
    else:
        try:
            rec = json.loads(out[i])
        except ValueError:
            return out
        if not isinstance(rec, dict) or not rec:
            return out
        key = r.choice(sorted(rec))
        if op == 5:
            rec[key] = r.choice(JUNK)
        elif op == 6:
            del rec[key]
        elif op == 7:
            rec["body"] = r.choice(JUNK) if r.random() < 0.5 else {**rec.get("body", {}), "z": r.choice(JUNK)}
        else:
            out[i] = json.dumps(r.choice(JUNK))
            return out
        out[i] = json.dumps(rec, default=str)
    return out


# ── fuzz: backups and anchors ────────────────────────────────────────────────


def test_fuzz_backup_verify_raises_only_backup_error(tmp_path: Path) -> None:
    made = backup.write(real_ledger(), "s", out=tmp_path, now=0)
    data, meta = made.path.read_text(), made.path.with_suffix(".json").read_text()
    for seed in range(ITERS):
        r = rng(seed)
        lines = mutate(r, data.splitlines())
        made.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if r.random() < 0.3:
            made.path.with_suffix(".json").unlink(missing_ok=True)  # no manifest: the chain alone must decide
        elif r.random() < 0.3:
            m = json.loads(meta)
            m[r.choice(sorted(m))] = r.choice(JUNK)
            made.path.with_suffix(".json").write_text(json.dumps(m, default=str))
        else:
            made.path.with_suffix(".json").write_text(meta)
        try:
            backup.verify(made.path)
        except backup.BackupError:
            pass
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"seed {seed}: {type(exc).__name__}: {exc}")


def test_fuzz_anchor_log_raises_only_backup_error(tmp_path: Path) -> None:
    path = tmp_path / "anchors.jsonl"
    good = json.dumps({"seq": 3, "digest": "ab" * 32, "ts": 1.0, "source": "s"})
    for seed in range(ITERS):
        r = rng(seed)
        blob = mutate(r, [good, good]) if r.random() < 0.6 else [r.choice([good, "{", "[]", "null", "\x00"])]
        raw = ("\n".join(blob) + "\n").encode("utf-8")
        if r.random() < 0.2:
            raw += bytes(r.randrange(256) for _ in range(8))  # not UTF-8
        path.write_bytes(raw)
        try:
            backup.load_anchors(path)
        except backup.BackupError:
            pass
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"seed {seed}: {type(exc).__name__}: {exc}\n{raw!r}")


# ── fuzz: Vortex feed ────────────────────────────────────────────────────────


def test_fuzz_sse_parser() -> None:
    pieces = [
        "data:",
        "data: ",
        "\n",
        "\n\n",
        "\r\n",
        ":",
        "event: x",
        '{"a":1}',
        "[1]",
        "null",
        "{",
        "}",
        "é",
    ]
    for seed in range(ITERS):
        r = rng(seed)
        text = "".join(r.choice(pieces) for _ in range(r.randint(0, 20)))
        try:
            events = live.parse_sse(text)
        except (relay.RelayError, ValueError):
            continue
        assert all(isinstance(e, dict) for e in events), f"seed {seed}: {text!r}"


def random_event(r: random.Random, eid: int) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "event_id": eid,
        "task_id": f"t{eid % 3}",
        "actor_id": "hermes-1",
        "event_type": r.choice(["created", "claimed", "blocked", "completed"]),
        "details": {},
    }
    if r.random() < 0.4:
        ev[r.choice(sorted(ev))] = r.choice(JUNK)
    if r.random() < 0.1:
        del ev[r.choice(sorted(ev))]
    return ev


def test_fuzz_relay_bad_batch_posts_nothing() -> None:
    for seed in range(ITERS):
        r = rng(seed)
        lg = Ledger(clock=ManualClock())
        batch = [random_event(r, i + 1) for i in range(r.randint(1, 6))]
        if r.random() < 0.2:
            batch.append(dict(batch[0]))  # duplicate
        try:
            relay.relay(lg, batch)
        except relay.RelayError:
            assert lg.entries == [], f"seed {seed}: posted before refusing"
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"seed {seed}: {type(exc).__name__}: {exc}\n{batch}")


# ── fuzz: linx ask ───────────────────────────────────────────────────────────

WORDS = (
    "approve reject halt stop resume back up rollback check memory put get remember the my all every "
    "repos pull update tell ask sebastian o0 to rm -rf / ; && | $(whoami) ` --registry --ledger x.jsonl "
    "T1 B59 retry nightly digest phone what is how does why find log brief task for explain because"
).split()


def parse(argv: list[str]) -> Any:
    return _parser().parse_args(argv)


def test_fuzz_ask_rules_stay_inside_the_allowlist(capsys: pytest.CaptureFixture[str]) -> None:
    for seed in range(ITERS * 3):
        r = rng(seed)
        text = " ".join(r.choice(WORDS) for _ in range(r.randint(1, 10)))
        try:
            plan = ask.interpret(text, parse=parse, pending=[("T1", "audit", "o0")])
        except ask.AskError:
            continue
        assert plan.argv[0] in ask.ALLOWED, f"seed {seed}: {text!r} -> {plan.argv}"
        assert not any(a.startswith("--registry") for a in plan.argv)
    capsys.readouterr()  # argparse may print usage for refused commands


def model_reply(content: Any, shape: int) -> Callable[..., Any]:
    bodies: list[Any] = [
        {"choices": [{"message": {"content": content}}]},
        {"choices": []},
        {"nope": 1},
        [content],
        {"choices": [{"message": None}]},
    ]

    def opener(req: Any, timeout: float) -> Any:
        return io.BytesIO(json.dumps(bodies[shape]).encode())

    return opener


def test_fuzz_model_replies_never_escape(capsys: pytest.CaptureFixture[str]) -> None:
    argvs = [
        ["status"],
        ["halt"],
        ["rm", "-rf", "/"],
        ["ask", "x"],
        ["status", "--registry", "x"],
        [],
        [1],
        "x",
    ]
    for seed in range(ITERS):
        r = rng(seed)
        content: Any = r.choice(
            [
                json.dumps({"argv": r.choice(argvs), "explain": "e"}),
                "sure! " + json.dumps({"argv": r.choice(argvs)}) + " hope that helps {",
                "{not json}",
                "no json at all",
                json.dumps([1, 2]),
                r.choice(JUNK),
            ]
        )
        try:
            plan = ask.interpret(
                "zzqx unusual request",
                parse=parse,
                llm_url="http://localhost:11434/v1",
                opener=model_reply(content, r.randrange(5) if r.random() < 0.3 else 0),
            )
        except ask.AskError:
            continue
        except Exception as exc:  # pragma: no cover
            pytest.fail(f"seed {seed}: {type(exc).__name__}: {exc} for {content!r}")
        assert plan.argv[0] in ask.ALLOWED, f"seed {seed}: {plan.argv}"
    capsys.readouterr()


# ── fuzz: repo expert search ─────────────────────────────────────────────────


def test_fuzz_expert_search_never_crashes(tmp_path: Path) -> None:
    root = tmp_path / "repos"
    (root / "LinX").mkdir(parents=True)
    (root / "LinX" / "README.md").write_text(
        "# LinX\n\nNo forks: seq and prev are unique.\n", encoding="utf-8"
    )
    db = expert.connect(tmp_path / "i.sqlite")
    try:
        expert.update(db, load(), root, extra=())
        alphabet = [
            '"',
            "'",
            "*",
            "(",
            ")",
            "-",
            "^",
            ":",
            "NEAR",
            "AND",
            "OR",
            "NOT",
            "é",
            "中",
            "forks",
            " ",
        ]
        for seed in range(ITERS):
            r = rng(seed)
            q = "".join(r.choice(alphabet) for _ in range(r.randint(0, 12)))
            expert.search(db, q, system=r.choice([None, "LinX", "x'; DROP TABLE chunks; --"]))
    finally:
        db.close()


# ── chaos: concurrent Supabase writers ───────────────────────────────────────


class FakeTable:
    """PostgREST over `unisync_events` with the unique seq/prev constraint and injected faults."""

    def __init__(self, r: random.Random, fault_rate: float) -> None:
        self.rows: list[dict[str, Any]] = []
        self.r = r
        self.fault_rate = fault_rate
        self.background = 0  # rows written by a writer we don't control

    def _foreign_write(self) -> None:
        other = Ledger.from_records(self.rows)
        env = other.post("hermes", Kind.MEMORY_WRITE, key=f"bg{self.background}", value=1, scope="shared")
        self.rows.append(env.to_dict())
        self.background += 1

    def __call__(self, req: Any, timeout: float = 0) -> Any:
        fault = self.r.random() < self.fault_rate
        if req.get_method() == "GET":
            if fault:
                raise urllib.error.URLError("connection reset")
            start = int(req.full_url.split("seq=gte.")[1].split("&")[0])
            return io.BytesIO(json.dumps(self.rows[start : start + 1000]).encode())
        if self.r.random() < 0.3:
            self._foreign_write()  # someone else got there first
        row = json.loads(req.data)
        head = self.rows[-1]["digest"] if self.rows else ""
        if row["seq"] != len(self.rows) or row["prev"] != head:
            raise urllib.error.HTTPError(req.full_url, 409, "conflict", {}, None)  # type: ignore[arg-type]
        if fault and self.r.random() < 0.5:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, None)  # type: ignore[arg-type]
        self.rows.append(row)
        if fault:
            raise TimeoutError("response lost after the write landed")
        return io.BytesIO(b"")


@pytest.mark.parametrize("seed", range(8))
def test_chaos_concurrent_supabase_writers_never_fork(seed: int, monkeypatch: pytest.MonkeyPatch) -> None:
    from lestinxor import unisync_supabase

    r = rng(seed)
    table = FakeTable(r, fault_rate=0.15)
    monkeypatch.setattr(unisync_supabase.urllib.request, "urlopen", table)
    writers = {
        name: unisync_supabase.SupabaseLedger("https://x.supabase.co", "k", retries=20)
        for name in ("sebastian", "jules", "claude")
    }
    landed = 0
    for i in range(40):
        name = r.choice(sorted(writers))
        try:
            writers[name].post(name, Kind.MEMORY_WRITE, key=f"{name}-{i}", value=i, scope="shared")
            landed += 1
        except (urllib.error.URLError, TimeoutError, ProtocolError):
            continue  # the caller sees the failure; the next sync shows whether it landed
    final = Ledger.from_records(table.rows)  # the shared table still verifies end to end
    assert final.verify()
    mine = [e for e in final.entries if not e.body.get("key", "").startswith("bg")]
    keys = [e.body["key"] for e in mine]
    assert landed <= len(keys) <= 40 and len(keys) == len(set(keys)), f"seed {seed}: duplicate posts"
    for w in writers.values():
        while True:
            try:
                w.sync()
                break
            except urllib.error.URLError:
                continue
        assert w.ledger.head == final.head, f"seed {seed}: a writer forked"


# ── chaos: live relay under network faults ───────────────────────────────────


@pytest.mark.parametrize("seed", range(8))
def test_chaos_live_relay_exactly_once(seed: int, tmp_path: Path) -> None:
    r = rng(seed)
    feed = [
        {
            "event_id": i,
            "task_id": f"t{i % 4}",
            "actor_id": "hermes-1",
            "event_type": ["created", "claimed", "blocked", "completed"][i % 4],
            "details": {"blocked_reason": "waiting"} if i % 4 == 2 else {},
        }
        for i in range(1, 31)
    ]
    led = live.FileLedger(tmp_path / "ledger.jsonl")
    real_post = led.post

    def flaky_post(*a: Any, **k: Any) -> Any:
        if r.random() < 0.05:
            raise ConnectionError("ledger write dropped")
        return real_post(*a, **k)

    led.post = flaky_post  # type: ignore[method-assign]

    def fetch(after: int) -> list[dict[str, Any]]:
        roll = r.random()
        if roll < 0.15:
            raise urllib.error.URLError("network down")
        if roll < 0.25:
            raise urllib.error.HTTPError("u", 503, "busy", {}, None)  # type: ignore[arg-type]
        if roll < 0.3:
            raise TimeoutError()
        older = r.sample(feed[:after], min(after, 2)) if after else []  # replays of old events are harmless
        return older + feed[after : after + r.randint(0, 5)]

    stats = live.run(led, fetch, interval=0, max_polls=400, sleep=lambda s: None)
    assert relay.cursor(led) == 30, f"seed {seed}: stuck at {relay.cursor(led)} ({stats})"
    writes = [e for e in led.ledger.entries if e.kind is Kind.MEMORY_WRITE]
    ids = [e.body["value"]["event_id"] for e in writes]
    assert ids == list(range(1, 31)), f"seed {seed}: not exactly once, in order: {ids}"
    escalated = [e.body["vortex_event"] for e in led.ledger.entries if e.kind is Kind.ESCALATE]
    blocked = [e["event_id"] for e in feed if e["event_type"] == "blocked"]
    assert escalated == blocked, f"seed {seed}: escalations lost or doubled: {escalated}"
    # what was written to disk replays to the same verified chain
    assert Ledger.from_jsonl((tmp_path / "ledger.jsonl").read_text()).head == led.ledger.head
