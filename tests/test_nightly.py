from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger

from linx import load, memory, nightly
from linx.cli import main

REG = load()
BACKLOG = """# Backlog
- [x] **B1 Done thing.** finished
- [ ] 🔒 **B2 Locked thing.** owner only
- [ ] **B3 Next thing.** do it
- [ ] **B4 After that.** later
- [ ] **B5 (PR open: #9) Under review.** waiting
"""
LESTINXOR = "- [x] **L1: old.**\n- [ ] **L3: `python -m lestinxor` smoke test.** cover it\n"
HORIZONTAL = "- [ ] 🔒 **H4: locked.**\n- [ ] **H2: `git_ops.py` uses `ENGINE_ROOT`.** fix\n"


def repos(root: Path) -> Path:
    for repo, text in (("Sebastian", BACKLOG), ("LESTINXOR-AGENT", LESTINXOR), ("HORIZONTAL", HORIZONTAL)):
        (root / repo / "docs").mkdir(parents=True)
        (root / repo / "docs" / "BACKLOG.md").write_text(text, encoding="utf-8")
    return root


def ledger() -> Ledger:
    return Ledger(clock=ManualClock())


def test_parse_backlog_formats() -> None:
    items = nightly.parse_backlog(
        BACKLOG + LESTINXOR + "- [ ] **P3-12 Leaderboard metrics** (spec)\n- [ ] **X6** Neuro\n"
    )
    by = {i.id: i for i in items}
    assert by["B1"].done and by["B2"].locked and by["B5"].in_review
    assert by["L3"].title == "`python -m lestinxor` smoke test"
    assert by["P3-12"].title == "Leaderboard metrics" and by["X6"].title == "Neuro"
    assert nightly.next_item(items).id == "B3"  # type: ignore[union-attr]
    assert nightly.next_item(items, {"B3", "B4"}).id == "L3"  # type: ignore[union-attr]
    assert nightly.next_item([]) is None


def test_plan_posts_one_task_per_repo_once(tmp_path: Path) -> None:
    lg, root = ledger(), repos(tmp_path)
    posted = nightly.plan(lg, REG, root, day="2026-11-02")
    assert posted == [
        "nightly/2026-11-02/lestinxor/L3",
        "nightly/2026-11-02/sebastian/B3",
        "nightly/2026-11-02/horizontal/H2",
    ]
    req = next(e for e in lg.entries if e.task_id == "nightly/2026-11-02/sebastian/B3")
    assert req.sender == "lestinxor"
    assert req.body["agent"] == "sebastian" and req.body["unattended"] is True
    assert req.body["repo"] == "jonnysteedman683-wq/Sebastian"
    assert nightly.plan(lg, REG, root, day="2026-11-02") == []
    assert nightly.plan(lg, REG, root, day="2026-11-03") == []  # still open: not re-posted next night
    assert lg.verify()


def test_plan_moves_on_after_done_and_retries_after_failure(tmp_path: Path) -> None:
    lg, root = ledger(), repos(tmp_path)
    nightly.plan(lg, REG, root, day="d1")
    lg.post("sebastian", Kind.TASK_CLAIM, "nightly/d1/sebastian/B3")
    lg.post("sebastian", Kind.TASK_RESULT, "nightly/d1/sebastian/B3", summary="ok")
    lg.post("jules", Kind.TASK_CLAIM, "nightly/d1/lestinxor/L3")
    lg.post("jules", Kind.TASK_FAILED, "nightly/d1/lestinxor/L3", reason="red")
    posted = nightly.plan(lg, REG, root, day="d2")
    assert "nightly/d2/sebastian/B4" in posted  # B3 done -> next item
    assert "nightly/d2/lestinxor/L3" in posted  # failed -> retried


def test_plan_skips_missing_backlogs(tmp_path: Path) -> None:
    assert nightly.plan(ledger(), REG, tmp_path, day="d") == []


def test_work_runs_command_and_reports_pr(tmp_path: Path) -> None:
    lg, root = ledger(), repos(tmp_path)
    nightly.plan(lg, REG, root, day="d")
    calls: list[tuple[list[str], Path]] = []

    def runner(argv: Sequence[str], cwd: Path, timeout: float, env: object = None) -> tuple[int, str]:
        calls.append((list(argv), cwd))
        return 0, "accepted\nhttps://github.com/jonnysteedman683-wq/Sebastian/pull/200\n"

    assert nightly.work(lg, REG, root, "sebastian", runner=runner) == "nightly/d/sebastian/B3"
    argv, cwd = calls[0]
    assert argv == ["sebastian", "evolve", "--repo", str(root / "Sebastian"), "--item", "B3", "--pr"]
    assert cwd == root / "Sebastian"
    assert lg.status("nightly/d/sebastian/B3") == "done"
    res = lg.entries[-1]
    assert res.body["pr"] == "https://github.com/jonnysteedman683-wq/Sebastian/pull/200"
    assert nightly.work(lg, REG, root, "sebastian", runner=runner) is None  # nothing left
    assert nightly.work(lg, REG, root, "jules", runner=runner) is None  # no unattended command


def test_work_reports_failure_with_output_tail(tmp_path: Path) -> None:
    lg, root = ledger(), repos(tmp_path)
    nightly.plan(lg, REG, root, day="d")
    nightly.work(lg, REG, root, "sebastian", runner=lambda a, c, t, e=None: (1, "gate failed: tests red"))
    assert lg.status("nightly/d/sebastian/B3") == "failed"
    assert lg.entries[-1].body["reason"] == "exit 1: gate failed: tests red"


def test_real_runner_handles_missing_binary_and_timeout(tmp_path: Path) -> None:
    code, out = nightly._run(["definitely-not-a-command-xyz"], tmp_path, 5)
    assert code == 127
    code, out = nightly._run(["python", "-c", "import time; time.sleep(5)"], tmp_path, 0.2)
    assert code == 124 and "timed out" in out


def test_digest(tmp_path: Path) -> None:
    lg, root = ledger(), repos(tmp_path)
    nightly.plan(lg, REG, root, day="1970-01-01")
    nightly.work(
        lg, REG, root, "sebastian", runner=lambda a, c, t, e=None: (0, "https://github.com/o/r/pull/7")
    )
    lg.post("o0", Kind.TASK_REQUEST, "T9", title="risky", requires_approval=True)
    lg.post("grok", Kind.ESCALATE, reason="watch this")
    lg.post("sebastian", Kind.MEMORY_WRITE, key="k", value="a")
    lg.post("horizontal", Kind.MEMORY_WRITE, key="k", value="b")
    text = nightly.digest(lg, "1970-01-01", memory_report=memory.consolidate(lg.entries))
    assert text.startswith("# LinX nightly digest: 1970-01-01")
    assert "✅ **sebastian** `B3` done (sebastian): PR https://github.com/o/r/pull/7" in text
    assert "📋 **lestinxor** `L3` open (jules): waiting for Jules/Claude to pick up" in text
    assert "Waiting on you: 1 approval(s)" in text and "`T9`" in text
    assert "grok: watch this" in text
    assert "conflict `shared/k`" in text
    assert "No nightly tasks" in nightly.digest(ledger(), "2000-01-01")


def test_cli_nightly_run_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = repos(tmp_path / "repos")
    led = str(tmp_path / "u.jsonl")
    monkeypatch.setattr(nightly, "_run", lambda a, c, t, e=None: (0, "https://github.com/o/r/pull/1"))
    monkeypatch.setattr(nightly.work, "__kwdefaults__", {"runner": nightly._run, "timeout": 3600.0})
    out = tmp_path / "digest.md"
    base = ["nightly", "--ledger", led, "--root", str(root), "--date", "1970-01-01"]
    assert main([*base, "run", "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "planned 3 task(s)" in text and "sebastian: ran nightly/1970-01-01/sebastian/B3" in text
    assert "PR https://github.com/o/r/pull/1" in out.read_text(encoding="utf-8")
    assert main([*base, "plan"]) == 0  # B3 is done, so Sebastian moves on; L3/H2 are still open
    assert capsys.readouterr().out == "planned 1 task(s)\n  nightly/1970-01-01/sebastian/B4\n"
    assert main([*base, "plan"]) == 0
    assert "planned 0 task(s)" in capsys.readouterr().out
    assert main([*base, "work", "--as", "sebastian"]) == 0
    assert "ran nightly/1970-01-01/sebastian/B4" in capsys.readouterr().out
    assert main([*base, "work", "--as", "sebastian"]) == 0
    assert "nothing to do" in capsys.readouterr().out
    assert main([*base, "digest"]) == 0
    backlog = root / "Sebastian" / "docs" / "BACKLOG.md"
    backlog.write_text(backlog.read_text(encoding="utf-8") + "- [ ] **B6 New.** x\n", encoding="utf-8")
    assert main([*base, "plan", "--as", "sebastian"]) == 2  # a worker may not request tasks
    assert "may not post task.request" in capsys.readouterr().err
    assert Ledger.from_jsonl(Path(led).read_text()).verify()
