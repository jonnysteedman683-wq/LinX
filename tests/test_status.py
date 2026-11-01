from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger

from linx import load, status
from linx.cli import main
from linx.relay import relay

FEED: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "vortex_events.json").read_text()
)


def fleet_ledger() -> Ledger:
    lg = Ledger(clock=ManualClock())
    lg.post("jonny", Kind.TASK_REQUEST, "T1", title="evolve a tool")
    lg.post("horizontal", Kind.TASK_CLAIM, "T1")
    lg.post("jonny", Kind.TASK_REQUEST, "T2", title="decode intent")
    lg.post("neurocore", Kind.TASK_CLAIM, "T2")
    lg.post("neurocore", Kind.TASK_RESULT, "T2", summary="ok")
    lg.post("o0", Kind.TASK_REQUEST, "T3", title="audit tasks.py", requires_approval=True)
    lg.post("lestinxor", Kind.TASK_REQUEST, "T4", title="search plans")
    lg.post("sebastian", Kind.TASK_CLAIM, "T4")
    lg.post("sebastian", Kind.TASK_FAILED, "T4", reason="tests red")
    relay(lg, FEED)
    return lg


def checkouts(root: Path) -> Path:
    for repo in ("Sebastian", "O0"):
        (root / repo).mkdir()
    return root


def by_name(fleet: status.FleetStatus) -> dict[str, status.SystemStatus]:
    return {s.name: s for s in fleet.systems}


def test_build_joins_checkouts_and_ledger(tmp_path: Path) -> None:
    fleet = status.build(load(), checkouts(tmp_path), fleet_ledger(), "test.jsonl")
    s = by_name(fleet)
    assert s["sebastian"].checkout == "present" and s["neurocore"].checkout == "MISSING"
    assert (s["horizontal"].claimed, s["horizontal"].done) == (1, 0)
    assert (s["neurocore"].claimed, s["neurocore"].done) == (0, 1)
    assert s["o0"].awaiting_approval == 1 and s["o0"].requested_open == 0
    assert s["lestinxor"].requested_open == 0 and s["sebastian"].failed == 1
    assert s["vortex"].last_seq == fleet.entries - 1
    assert not s["sentinel"].agent and s["sentinel"].last_seq is None
    assert fleet.verified is True
    assert fleet.approvals == [status.Approval("T3", "audit tasks.py", "o0")]
    assert [e.reason for e in fleet.escalations] == ["Vortex task t-aaa blocked: vault locked"]
    assert fleet.vortex == {"completed": 1, "open": 1}


def test_escalations_are_capped_to_the_latest(tmp_path: Path) -> None:
    lg = Ledger(clock=ManualClock())
    for i in range(8):
        lg.post("grok", Kind.ESCALATE, reason=f"r{i}")
    fleet = status.build(load(), tmp_path, lg, "x", escalation_limit=3)
    assert [e.reason for e in fleet.escalations] == ["r5", "r6", "r7"]


def test_without_a_ledger_only_checkouts(tmp_path: Path) -> None:
    fleet = status.build(load(), checkouts(tmp_path))
    assert fleet.ledger == "none" and fleet.verified is None and fleet.entries == 0
    out = status.render(fleet)
    assert "ledger: none" in out and "sebastian" in out


def test_render_shows_numbers_approvals_vortex_and_escalations(tmp_path: Path) -> None:
    out = status.render(status.build(load(), checkouts(tmp_path), fleet_ledger(), "u.jsonl"))
    assert "verified" in out and "pending approvals: 1" in out
    assert "T3  audit tasks.py  (from o0)" in out
    assert "vortex tasks: completed 1, open 1" in out
    assert "vortex: Vortex task t-aaa blocked: vault locked" in out
    row = next(line for line in out.splitlines() if line.startswith("horizontal "))
    assert row.split()[3:8] == ["1", "0", "0", "0", "0"]


def test_cli_status_text_json_and_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("UNISYNC_SUPABASE_URL", raising=False)
    (tmp_path / "repos").mkdir()
    root = checkouts(tmp_path / "repos")
    led = tmp_path / "u.jsonl"
    led.write_text(fleet_ledger().to_jsonl())
    assert main(["status", "--root", str(root), "--ledger", str(led)]) == 0
    assert "pending approvals: 1" in capsys.readouterr().out
    assert main(["status", "--root", str(root), "--ledger", str(led), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["verified"] is True and data["approvals"][0]["task_id"] == "T3"
    assert main(["status", "--root", str(root)]) == 0
    assert "ledger: none" in capsys.readouterr().out
    lines = led.read_text().splitlines()
    rec = json.loads(lines[1])
    rec["body"] = {"tampered": True}
    led.write_text("\n".join([lines[0], json.dumps(rec), *lines[2:]]) + "\n")
    assert main(["status", "--root", str(root), "--ledger", str(led)]) == 2
    assert "cannot read ledger" in capsys.readouterr().err
