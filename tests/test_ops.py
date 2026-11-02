from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger

from linx import control, load, nightly, ops
from linx.ask import interpret
from linx.cli import _parser, main

REG = load()


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, name: str, remote_root: Path) -> Path:
    """A checkout with an upstream remote (bare repo), one commit, tracking origin/main."""
    bare = remote_root / f"{name}.git"
    git(remote_root, "init", "-q", "--bare", "-b", "main", str(bare))
    work = root / name
    git(root, "clone", "-q", str(bare), str(work))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false")):
        git(work, "config", k, v)
    (work / "README.md").write_text("hello\n")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "first")
    git(work, "push", "-q", "-u", "origin", "main")
    git(work, "remote", "set-head", "origin", "main")
    return work


def push_upstream(remote_root: Path, name: str, tmp: Path) -> None:
    other = tmp / f"other-{name}"
    git(tmp, "clone", "-q", str(remote_root / f"{name}.git"), str(other))
    for k, v in (("user.email", "o@example.com"), ("user.name", "o"), ("commit.gpgsign", "false")):
        git(other, "config", k, v)
    (other / "new.txt").write_text("x\n")
    git(other, "add", ".")
    git(other, "commit", "-q", "-m", "upstream change")
    git(other, "push", "-q")


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    root, remotes = tmp_path / "repos", tmp_path / "remotes"
    root.mkdir()
    remotes.mkdir()
    for name in ("Sebastian", "O0", "HORIZONTAL"):
        make_repo(root, name, remotes)
    return root, remotes


def test_repos_reports_git_state_and_pull_is_fast_forward_only(
    world: tuple[Path, Path], tmp_path: Path
) -> None:
    root, remotes = world
    for name in ("Sebastian", "O0", "HORIZONTAL"):
        push_upstream(remotes, name, tmp_path)
    (root / "O0" / "dirty.txt").write_text("wip\n")  # O0 dirty: must not be pulled
    git(root / "HORIZONTAL", "checkout", "-q", "-b", "feature")  # not on default branch: not pulled
    git(root / "HORIZONTAL", "branch", "-q", "--set-upstream-to", "origin/main")

    before = {r.system: r for r in ops.repos(REG, root, fetch=True)}
    assert before["sebastian"].behind == 1 and before["sebastian"].ahead == 0
    assert before["o0"].dirty == 1 and before["o0"].needs_attention
    assert not before["lestinxor"].present

    after = {r.system: r for r in ops.repos(REG, root, pull=True)}
    assert after["sebastian"].action == "pulled (fast-forward)" and after["sebastian"].behind == 0
    assert (root / "Sebastian" / "new.txt").exists()
    assert after["o0"].action == "not pulled: uncommitted changes"
    assert after["horizontal"].action.startswith("not pulled: on feature")
    text = ops.render_repos(list(after.values()))
    assert "lestinxor         MISSING" in text and "-> pulled (fast-forward)" in text


def test_repo_without_upstream_and_failed_fetch(tmp_path: Path) -> None:
    work = tmp_path / "Sebastian"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "remote", "add", "origin", str(tmp_path / "no-such-remote.git"))
    state = ops.repo_state("sebastian", work, fetch=True)
    assert state.present and state.ahead is None and state.action.startswith("fetch failed")


def test_backlog_overview_with_parked_items(tmp_path: Path) -> None:
    (tmp_path / "Sebastian" / "docs").mkdir(parents=True)
    (tmp_path / "Sebastian" / "docs" / "BACKLOG.md").write_text(
        "- [x] **B1 done.**\n- [ ] 🔒 **B2 locked.**\n- [ ] **B3 next.**\n- [ ] **B4 later.**\n",
        encoding="utf-8",
    )
    lg = Ledger(clock=ManualClock())
    for day in ("d1", "d2", "d3"):
        nightly.plan(lg, REG, tmp_path, day=day)
        nightly.work(lg, REG, tmp_path, "sebastian", runner=lambda *a: (1, "red"))
    text = ops.backlog(REG, tmp_path, lg)
    assert "sebastian: 2 open, 1 locked, 0 in review, 1 done" in text
    assert "next: B4 later" in text and "parked after 3 failures: B3" in text
    assert ops.backlog(REG, tmp_path, None, only="sebastian").splitlines()[1] == "  next: B3 next"
    assert ops.backlog(REG, tmp_path / "nowhere") == "no backlogs found"


def test_timeline_filters() -> None:
    lg = Ledger(clock=ManualClock())
    lg.post("o0", Kind.TASK_REQUEST, "T1", title="audit")
    lg.post("sebastian", Kind.TASK_CLAIM, "T1")
    lg.post("sebastian", Kind.TASK_RESULT, "T1", summary="done it")
    lg.post("jonny", Kind.MEMORY_WRITE, key="goal", value="ship")
    text = ops.timeline(lg)
    assert "#0     o0              task.request      [T1] audit" in text
    assert "shared/goal = ship" in text
    assert ops.timeline(lg, agent="sebastian").count("\n") == 1
    assert "done it" in ops.timeline(lg, task="T1", last=1)
    assert ops.timeline(lg, agent="nobody") == "no matching entries"


def test_find_searches_text_files_and_skips_junk(tmp_path: Path) -> None:
    (tmp_path / "Sebastian" / "src").mkdir(parents=True)
    (tmp_path / "Sebastian" / "src" / "gate.py").write_text("def approval_gate():\n    pass\n")
    (tmp_path / "Sebastian" / "node_modules").mkdir()
    (tmp_path / "Sebastian" / "node_modules" / "x.js").write_text("approval_gate\n")
    (tmp_path / "Sebastian" / "image.png").write_bytes(b"approval_gate")
    assert ops.find(REG, tmp_path, "approval_gate") == ["Sebastian/src/gate.py:1: def approval_gate():"]
    assert ops.find(REG, tmp_path, "APPROVAL_GATE(", limit=1)  # invalid regex falls back to literal
    assert ops.find(REG, tmp_path, "approval_gate", only="o0") == []


def test_assign_posts_owner_task() -> None:
    lg = Ledger(clock=ManualClock())
    tid = ops.assign(lg, "Fix the flaky eval test!", "sebastian")
    assert tid.startswith("jonny/fix-the-flaky-eval-test-")
    assert lg.status(tid) == "open" and lg.entries[-1].body["agent"] == "sebastian"
    tid2 = ops.assign(lg, "Deploy", "hermes", approval=True)
    assert lg.status(tid2) == "awaiting_approval" and tid2 != tid


def test_brief(world: tuple[Path, Path]) -> None:
    root, _ = world
    (root / "O0" / "wip.txt").write_text("x")
    lg = Ledger(clock=ManualClock())
    lg.post("o0", Kind.TASK_REQUEST, "T1", title="audit", requires_approval=True)
    lg.post("sebastian", Kind.MEMORY_WRITE, key="k", value="a")
    lg.post("horizontal", Kind.MEMORY_WRITE, key="k", value="b")
    control.halt(lg, "storm")
    text = ops.brief(REG, root, lg, day="1970-01-01")
    assert "⛔ HALTED: storm" in text and "verified" in text
    assert "waiting on you: 1" in text and "T1  audit" in text
    assert "memory: 1 facts, 1 conflicts" in text  # the halt flag is not a shared fact
    assert "3 present, 1 need attention, missing: lestinxor" in text and "o0: 1 dirty" in text


@pytest.mark.parametrize(
    "text, argv",
    [
        ("good morning", ["brief"]),
        ("catch me up", ["brief"]),
        ("are my repos up to date", ["repos", "--fetch"]),
        ("any uncommitted changes in my repos", ["repos"]),
        ("update all my repos", ["repos", "--pull"]),
        ("what's next for horizontal", ["backlog", "horizontal"]),
        ("show the backlog", ["backlog"]),
        ("what did sebastian do", ["log", "--agent", "sebastian"]),
        ("show recent history", ["log"]),
        ("where is approval_gate used", ["find", "approval_gate"]),
        ("test sebastian", ["check", "sebastian"]),
        (
            "ask sebastian to fix the flaky eval test.",
            ["task", "fix the flaky eval test", "--for", "sebastian"],
        ),
    ],
)
def test_ask_understands_new_commands(text: str, argv: list[str]) -> None:
    assert interpret(text, parse=lambda a: _parser().parse_args(a)).argv == argv


def test_ask_owner_flags_for_new_commands() -> None:
    from linx.ask import Plan

    assert Plan(["task", "x", "--for", "s"], "", "rules").owner_action
    assert Plan(["repos", "--pull"], "", "rules").owner_action
    assert not Plan(["repos", "--fetch"], "", "rules").owner_action
    assert not Plan(["brief"], "", "rules").owner_action


def test_cli_ops(world: tuple[Path, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root, _ = world
    led = tmp_path / "u.jsonl"
    r = ["--root", str(root)]
    assert main(["repos", *r]) == 0 and "sebastian" in capsys.readouterr().out
    assert main(["find", "hello", *r]) == 0 and "README.md:1: hello" in capsys.readouterr().out
    assert main(["find", "zzzz-nope", *r]) == 1
    assert main(["task", "fix it", "--for", "sebastian", "--ledger", str(led)]) == 0
    assert "posted jonny/fix-it-" in capsys.readouterr().out
    assert main(["log", "--ledger", str(led)]) == 0 and "fix it" in capsys.readouterr().out
    assert main(["brief", *r, "--ledger", str(led), "--date", "1970-01-01"]) == 0
    assert "LinX brief: 1970-01-01" in capsys.readouterr().out
    assert main(["backlog", *r, "--ledger", str(led)]) == 0
    assert main(["check", "nonexistent", *r]) == 2
    assert main(["check", "o0", *r]) in (0, 1)  # runs O0's real check command (pytest) on an empty repo
    assert main(["check", "lestinxor", *r]) == 1 and "MISSING" in capsys.readouterr().out
    assert main(["task", "x", "--for", "s", "--ledger", str(tmp_path / "bad" / "x.jsonl")]) == 2
