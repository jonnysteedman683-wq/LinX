from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger, ProtocolError

from linx import control, load, memory, nightly, notify, status
from linx.cli import main

REG = load()
BACKLOG = "- [ ] **B3 Next.** x\n- [ ] **B4 After.** y\n"


def ledger() -> Ledger:
    return Ledger(clock=ManualClock())


def repos(root: Path) -> Path:
    (root / "Sebastian" / "docs").mkdir(parents=True)
    (root / "Sebastian" / "docs" / "BACKLOG.md").write_text(BACKLOG, encoding="utf-8")
    return root


# ── kill switch ──────────────────────────────────────────────────────────────


def test_halt_and_resume_are_owner_only() -> None:
    lg = ledger()
    assert control.halt_state(lg) is None
    lg.post(
        "grok", Kind.MEMORY_WRITE, key="halt", value={"halted": True, "reason": "rogue"}, scope="linx/control"
    )
    assert control.halt_state(lg) is None  # an agent cannot halt the fleet
    control.halt(lg, "maintenance")
    assert control.halt_state(lg) == {"halted": True, "reason": "maintenance"}
    assert lg.entries[-1].kind is Kind.ESCALATE and lg.entries[-1].sender == "jonny"
    lg.post("sebastian", Kind.MEMORY_WRITE, key="halt", value={"halted": False}, scope="linx/control")
    assert control.halt_state(lg) is not None  # nor lift a halt
    control.resume(lg, "done")
    assert control.halt_state(lg) is None


def test_halt_stops_nightly_and_pauses_memory(tmp_path: Path) -> None:
    lg, root = ledger(), repos(tmp_path)
    control.halt(lg, "stop")
    with pytest.raises(control.Halted, match="halted by jonny: stop"):
        nightly.plan(lg, REG, root, day="d")
    with pytest.raises(control.Halted):
        nightly.work(lg, REG, root, "sebastian", runner=lambda *a: (0, ""))
    n = len(lg.entries)
    sleeps: list[float] = []
    assert memory.run(lg, "coordinator-bot", every=5, max_rounds=2, sleep=sleeps.append) == 0
    assert len(lg.entries) == n and sleeps == [5]
    text = nightly.digest(lg, "1970-01-01")
    assert "⛔ HALTED" in text
    assert status.build(REG, root, lg, "x").halted == "stop"
    assert status.render(status.build(REG, root, lg, "x")).startswith("⛔ HALTED by jonny: stop")
    control.resume(lg)
    assert nightly.plan(lg, REG, root, day="d") == ["nightly/d/sebastian/B3"]


# ── approvals ────────────────────────────────────────────────────────────────


def test_pending_approve_reject() -> None:
    lg = ledger()
    lg.post("o0", Kind.TASK_REQUEST, "T1", title="audit", requires_approval=True)
    lg.post("o0", Kind.TASK_REQUEST, "T2", title="deploy", requires_approval=True)
    assert control.pending(lg) == [("T1", "audit", "o0"), ("T2", "deploy", "o0")]
    control.decide(lg, "T1", True, "ok")
    control.decide(lg, "T2", False, "too risky")
    assert lg.status("T1") == "approved" and lg.status("T2") == "rejected"
    assert lg.entries[-1].body == {"approved": False, "reason": "too risky"}
    with pytest.raises(ProtocolError, match="no pending approval"):
        control.decide(lg, "T1", True)
    assert control.pending(lg) == []


# ── retry cap + feedback ─────────────────────────────────────────────────────


def fail_night(lg: Ledger, root: Path, day: str, reason: str) -> None:
    nightly.plan(lg, REG, root, day=day)
    nightly.work(lg, REG, root, "sebastian", runner=lambda *a: (1, reason))


def test_failed_item_is_retried_with_feedback_then_parked_and_escalated(tmp_path: Path) -> None:
    lg, root = ledger(), repos(tmp_path)
    seen: list[tuple[list[str], Any]] = []
    fail_night(lg, root, "d1", "tests red")
    fail_night(lg, root, "d2", "lint failed")

    def runner(argv: Any, cwd: Any, timeout: Any, env: Any = None) -> tuple[int, str]:
        seen.append((list(argv), env))
        return 1, "gate failed"

    nightly.plan(lg, REG, root, day="d3")
    req = next(e for e in lg.entries if e.task_id == "nightly/d3/sebastian/B3")
    assert req.body["attempt"] == 3
    assert req.body["previous_failures"] == ["exit 1: tests red", "exit 1: lint failed"]
    nightly.work(lg, REG, root, "sebastian", runner=runner)
    assert seen[0][1] == {"LINX_FEEDBACK": "exit 1: tests red | exit 1: lint failed"}
    assert "B3` failed (sebastian, attempt 3)" in nightly.digest(lg, "d3")

    posted = nightly.plan(lg, REG, root, day="d4")  # B3 failed 3x: parked, B4 next
    assert posted == ["nightly/d4/sebastian/B4"]
    esc = [e for e in lg.entries if e.kind is Kind.ESCALATE and "nightly_parked" in e.body]
    assert len(esc) == 1 and esc[0].body["nightly_parked"] == "sebastian/B3"
    assert "linx nightly retry sebastian B3" in esc[0].body["reason"]
    lg.post("sebastian", Kind.TASK_CLAIM, "nightly/d4/sebastian/B4")
    lg.post("sebastian", Kind.TASK_RESULT, "nightly/d4/sebastian/B4", summary="ok")
    nightly.plan(lg, REG, root, day="d5")
    assert len([e for e in lg.entries if e.kind is Kind.ESCALATE and "nightly_parked" in e.body]) == 1  # once

    nightly.retry(lg, "sebastian", "B3")  # owner resets it
    assert nightly.failures(lg, "sebastian", "B3") == []
    assert nightly.plan(lg, REG, root, day="d6") == ["nightly/d6/sebastian/B3"]
    lg.post("grok", Kind.MEMORY_WRITE, key="retry:sebastian/B4", value=999, scope="linx/control")
    assert nightly.failures(lg, "sebastian", "B4") == []  # (no failures anyway; agent marks are ignored)


def test_feedback_placeholder_in_command(tmp_path: Path) -> None:
    from dataclasses import replace

    lg, root = ledger(), repos(tmp_path)
    reg = replace(
        REG,
        systems=tuple(
            replace(s, nightly_command=("agent", "--hint", "{feedback}")) if s.name == "sebastian" else s
            for s in REG.systems
        ),
    )
    nightly.plan(lg, reg, root, day="d1")
    nightly.work(lg, reg, root, "sebastian", runner=lambda *a: (1, "boom"))
    nightly.plan(lg, reg, root, day="d2")
    seen: list[list[str]] = []
    nightly.work(lg, reg, root, "sebastian", runner=lambda argv, *a: (seen.append(list(argv)), (0, ""))[1])
    assert seen[0] == ["agent", "--hint", "exit 1: boom"]


def test_real_runner_passes_env(tmp_path: Path) -> None:
    code, out = nightly._run(
        ["python", "-c", "import os; print(os.environ['LINX_FEEDBACK'])"],
        tmp_path,
        10,
        {"LINX_FEEDBACK": "hello"},
    )
    assert code == 0 and out.strip() == "hello"


# ── phone push ───────────────────────────────────────────────────────────────


def test_build_push_priorities_and_truncation() -> None:
    quiet = notify.build("# LinX nightly digest: 2026-11-02\nall good", approvals=0, failures=0, halted=False)
    assert (quiet.title, quiet.priority, quiet.tags) == (
        "LinX 2026-11-02: all quiet",
        "default",
        "white_check_mark",
    )
    busy = notify.build("# LinX nightly digest: d", approvals=2, failures=1, halted=False)
    assert busy.title == "LinX d: 2 to approve, 1 failed" and busy.priority == "high"
    stop = notify.build("# LinX nightly digest: d", approvals=0, failures=0, halted=True)
    assert stop.priority == "urgent" and "HALTED" in stop.title
    big = notify.build(
        "# LinX nightly digest: d\n" + "line ✅\n" * 2000, approvals=0, failures=0, halted=False
    )
    assert len(big.body.encode()) <= notify.MAX_BYTES + 60 and big.body.endswith("on the PC)")


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        pass


def test_send_posts_to_ntfy_with_headers() -> None:
    seen: dict[str, Any] = {}

    def opener(req: Any, timeout: float) -> _Resp:
        seen.update(url=req.full_url, data=req.data, headers=dict(req.header_items()))
        return _Resp(b"{}")

    notify.send(
        notify.Push("LinX d: all quiet", "body ✅", "default", "bell"),
        "https://ntfy.sh/topic",
        "tok",
        opener=opener,
    )
    assert seen["url"] == "https://ntfy.sh/topic" and seen["data"] == "body ✅".encode()
    h = {k.lower(): v for k, v in seen["headers"].items()}
    assert (
        h["title"] == "LinX d: all quiet"
        and h["priority"] == "default"
        and h["authorization"] == "Bearer tok"
    )
    with pytest.raises(Exception, match="loopback"):
        notify.send(notify.Push("t", "b", "default", "bell"), "http://ntfy.example/topic", opener=opener)


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_control_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    led = str(tmp_path / "u.jsonl")
    base = ["--ledger", led]
    lg = ledger()
    lg.post("o0", Kind.TASK_REQUEST, "T1", title="audit", requires_approval=True)
    lg.post("o0", Kind.TASK_REQUEST, "T2", title="deploy", requires_approval=True)
    Path(led).write_text(lg.to_jsonl())
    assert main(["pending", *base]) == 0
    assert "T1  audit  (from o0)" in capsys.readouterr().out
    assert main(["approve", "T1", *base, "--note", "ok"]) == 0
    assert main(["reject", "T2", *base, "--reason", "no"]) == 0
    assert main(["approve", "T1", *base]) == 2
    assert main(["halt", *base, "--reason", "storm"]) == 0
    root = repos(tmp_path / "repos")
    assert main(["nightly", *base, "--root", str(root), "--date", "1970-01-01", "run"]) == 3
    assert "halted by jonny: storm" in capsys.readouterr().err
    assert main(["resume", *base]) == 0
    monkeypatch.delenv("LINX_NTFY_URL", raising=False)
    assert main(["notify", *base]) == 2

    pushed: list[Any] = []
    monkeypatch.setenv("LINX_NTFY_URL", "https://ntfy.sh/t")
    monkeypatch.setattr(notify, "send", lambda push, url, token="": pushed.append((push, url)))
    assert main(["notify", *base, "--date", "1970-01-01"]) == 0
    assert pushed[0][1] == "https://ntfy.sh/t"
    monkeypatch.setattr(nightly, "_run", lambda *a: (0, "https://github.com/o/r/pull/3"))
    monkeypatch.setattr(nightly.work, "__kwdefaults__", {"runner": nightly._run, "timeout": 3600.0})
    assert main(["nightly", *base, "--root", str(root), "--date", "1970-01-01", "run"]) == 0
    assert "pushed digest to phone" in capsys.readouterr().out
    assert len(pushed) == 2

    def broken(*a: Any, **k: Any) -> None:
        raise OSError("network down")

    monkeypatch.setattr(notify, "send", broken)
    assert main(["notify", *base]) == 1  # a failed push is reported, not raised
    assert main(["nightly", *base, "--root", str(root), "retry", "sebastian", "B3"]) == 0
    assert Ledger.from_jsonl(Path(led).read_text()).verify()
