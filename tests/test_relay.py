from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger

from linx.cli import main
from linx.relay import RelayError, cursor, relay, tasks

FEED: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "vortex_events.json").read_text()
)


def ledger() -> Ledger:
    return Ledger(clock=ManualClock())


def test_scripted_feed_replays_into_a_verifying_ledger() -> None:
    lg = ledger()
    assert relay(lg, FEED) == 6
    assert lg.verify()
    replayed = Ledger.from_jsonl(lg.to_jsonl())
    assert replayed.verify()
    assert cursor(replayed) == 6
    state = tasks(replayed)
    assert state["t-aaa"]["status"] == "completed" and state["t-aaa"]["details"] == {"result": "3 cards"}
    assert state["t-bbb"]["status"] == "open" and state["t-bbb"]["actor"] == "claude-operator"


def test_every_envelope_is_from_vortex_and_never_dispatches() -> None:
    lg = ledger()
    relay(lg, FEED)
    assert {e.sender for e in lg.entries} == {"vortex"}
    assert {e.kind for e in lg.entries} == {Kind.MEMORY_WRITE, Kind.ESCALATE}
    assert lg.tasks() == []


def test_blocked_escalates_once() -> None:
    lg = ledger()
    relay(lg, FEED)
    escalations = [e for e in lg.entries if e.kind is Kind.ESCALATE]
    assert len(escalations) == 1
    assert escalations[0].body["reason"] == "Vortex task t-aaa blocked: vault locked"
    assert escalations[0].body["vortex_task"] == "t-aaa"


def test_pending_approval_shows_until_approved() -> None:
    lg = ledger()
    relay(lg, FEED[:3])
    assert tasks(lg)["t-bbb"]["status"] == "pending_approval"


def test_relay_is_idempotent_and_resumes_from_cursor() -> None:
    lg = ledger()
    assert relay(lg, FEED[:4]) == 4
    n = len(lg.entries)
    assert relay(lg, FEED[:4]) == 0 and len(lg.entries) == n
    assert relay(lg, reversed(FEED)) == 2
    assert cursor(lg) == 6 and lg.verify()


@pytest.mark.parametrize(
    "bad, msg",
    [
        ({"event_id": 0}, "event_id"),
        ({"event_id": True}, "event_id"),
        ({"task_id": ""}, "task_id"),
        ({"actor_id": None}, "actor_id"),
        ({"event_type": "exploded"}, "event_type"),
        ({"details": []}, "details"),
    ],
)
def test_malformed_event_posts_nothing(bad: dict[str, Any], msg: str) -> None:
    lg = ledger()
    feed = [dict(e) for e in FEED]
    feed[-1].update(bad)
    with pytest.raises(RelayError, match=msg):
        relay(lg, feed)
    assert lg.entries == []


def test_duplicate_event_ids_rejected() -> None:
    with pytest.raises(RelayError, match="duplicate"):
        relay(ledger(), [FEED[0], FEED[0]])


def test_cli_relay_appends_to_a_ledger_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    events = tmp_path / "events.json"
    events.write_text(json.dumps(FEED[:2]))
    out = tmp_path / "ledger.jsonl"
    assert main(["relay", str(events), "--ledger", str(out)]) == 0
    assert "relayed 2" in capsys.readouterr().out
    events.write_text(json.dumps(FEED))
    assert main(["relay", str(events), "--ledger", str(out)]) == 0
    assert "relayed 4" in capsys.readouterr().out
    lg = Ledger.from_jsonl(out.read_text())
    assert lg.verify() and cursor(lg) == 6
    events.write_text('[{"event_id": -1}]')
    assert main(["relay", str(events), "--ledger", str(out)]) == 2
