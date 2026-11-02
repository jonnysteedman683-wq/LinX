from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger

from linx import memory
from linx.cli import main
from linx.relay import relay

FEED: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "vortex_events.json").read_text()
)


def mem(lg: Ledger, sender: str, key: str, value: Any, scope: str = "shared") -> None:
    lg.post(sender, Kind.MEMORY_WRITE, key=key, value=value, scope=scope)


def scenario() -> Ledger:
    lg = Ledger(clock=ManualClock())
    mem(lg, "o0", "old_note", "first boot")  # seq 0, goes stale
    mem(lg, "sebastian", "build_cmd", "python -m pytest")
    mem(lg, "horizontal", "build_cmd", "python -m compileall -q .")  # conflict
    mem(lg, "o0", "goal", "ship linx")
    mem(lg, "lestinxor", "plan", {"steps": ["read goal", "unblock vortex/task/t-aaa"]})  # links
    mem(lg, "sebastian", "model_a", "same-model")
    mem(lg, "sebastian", "model_b", "same-model")  # duplicate
    relay(lg, FEED)  # vortex scope, incl. vortex/task/t-aaa
    return lg


def test_index_keeps_full_history_in_ledger_order() -> None:
    facts = memory.index(scenario().entries)
    build = facts["shared/build_cmd"]
    assert [(w.seq, w.sender) for w in build.history] == [(1, "sebastian"), (2, "horizontal")]
    assert build.current.value == "python -m compileall -q ." and build.writers == ["horizontal", "sebastian"]
    assert "vortex/task/t-aaa" in facts


def test_consolidate_finds_conflicts_stale_duplicates_links() -> None:
    lg = scenario()
    r = memory.consolidate(lg.entries, stale_after=10)
    assert r.head == len(lg.entries) - 1
    assert [c.ref for c in r.conflicts] == ["shared/build_cmd"]
    assert r.conflicts[0].values == {
        "horizontal": "python -m compileall -q .",
        "sebastian": "python -m pytest",
    }
    assert "shared/old_note" in r.stale and "shared/plan" not in r.stale
    assert r.duplicates == [["shared/model_a", "shared/model_b"]]
    assert r.links["shared/plan"] == ["shared/goal", "vortex/task/t-aaa"]


def test_same_agent_rewriting_is_not_a_conflict_and_agreement_is_not_either() -> None:
    lg = Ledger(clock=ManualClock())
    mem(lg, "o0", "k", 1)
    mem(lg, "o0", "k", 2)
    mem(lg, "sebastian", "j", "x")
    mem(lg, "horizontal", "j", "x")
    assert memory.consolidate(lg.entries).conflicts == []


def test_conflict_window_forgets_old_disagreement() -> None:
    lg = Ledger(clock=ManualClock())
    mem(lg, "horizontal", "k", "old")
    for _ in range(memory.CONFLICT_WINDOW):
        mem(lg, "sebastian", "k", "new")
    assert memory.consolidate(lg.entries).conflicts == []


def test_cards_match_vault_format() -> None:
    out = memory.cards(scenario().entries, memory.consolidate(scenario().entries, stale_after=10))
    card = out["mem_shared_build_cmd.md"]
    assert card.startswith("---\nid: mem_shared_build_cmd\n")
    assert "type: unisync_memory" in card and 'up: "[[000_UniSync_Memory_MOC]]"' in card
    assert '"memory/conflict"' in card and "### ⚠️ Conflicting values" in card
    assert "- #2 horizontal\n- #1 sebastian" in card
    plan = out["mem_shared_plan.md"]
    assert "[[mem_shared_goal]]" in plan and "[[mem_vortex_task_t_aaa]]" in plan and "```json" in plan
    assert "[[mem_shared_model_b]]" in out["mem_shared_model_a.md"]
    moc = out["000_UniSync_Memory_MOC.md"]
    assert "1 conflicts" in moc and "## vortex" in moc and "[[mem_shared_goal]]" in moc


def test_write_cards_refuses_a_live_vault(tmp_path: Path) -> None:
    (tmp_path / "vault" / ".obsidian").mkdir(parents=True)
    with pytest.raises(ValueError, match="vault root"):
        memory.write_cards(scenario().entries, tmp_path / "vault")
    paths = memory.write_cards(scenario().entries, tmp_path / "staging")
    assert (tmp_path / "staging" / "000_UniSync_Memory_MOC.md") in paths


def test_post_is_active_and_idempotent() -> None:
    lg = scenario()
    first = memory.post(lg, "coordinator-bot", memory.consolidate(lg.entries))
    assert first == 2  # one escalation + one summary
    esc = [e for e in lg.entries if e.kind is Kind.ESCALATE and "memory_ref" in e.body]
    assert esc[0].body["memory_ref"] == "shared/build_cmd" and esc[0].sender == "coordinator-bot"
    summary = lg.memory(memory.SUMMARY_SCOPE)["consolidation"]
    assert (
        summary["conflicts"] == ["shared/build_cmd"]
        and summary["facts"] == memory.consolidate(lg.entries).facts
    )
    assert memory.post(lg, "coordinator-bot", memory.consolidate(lg.entries)) == 0
    mem(lg, "neurocore", "build_cmd", "pnpm test")  # the conflict changes: escalate again, new summary
    assert memory.post(lg, "coordinator-bot", memory.consolidate(lg.entries)) == 2
    assert lg.verify()


def test_summary_scope_is_not_itself_consolidated() -> None:
    lg = scenario()
    memory.post(lg, "coordinator-bot", memory.consolidate(lg.entries))
    r = memory.consolidate(lg.entries)
    assert not any(ref.startswith(memory.SUMMARY_SCOPE) for g in r.duplicates for ref in g)
    assert all(not ref.startswith(memory.SUMMARY_SCOPE) for ref in r.links)


def test_cli_memory_round_trip(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    led = str(tmp_path / "u.jsonl")
    base = ["memory", "--ledger", led]
    assert main([*base, "put", "build_cmd", "python -m pytest", "--as", "sebastian"]) == 0
    assert main([*base, "put", "build_cmd", '"pnpm test"', "--as", "neurocore"]) == 0
    assert main([*base, "put", "cfg", '{"a": 1}', "--as", "o0", "--scope", "o0"]) == 0
    capsys.readouterr()
    assert main([*base, "get", "build_cmd"]) == 0
    assert capsys.readouterr().out.strip() == '#1 neurocore: "pnpm test"'
    assert main([*base, "history", "o0/cfg"]) == 0
    assert '{"a": 1}' in capsys.readouterr().out
    assert main([*base, "get", "nope"]) == 1
    assert main([*base, "put", "k", "v", "--as", "mallory"]) == 2
    capsys.readouterr()
    assert main([*base, "check"]) == 0
    assert "conflict  shared/build_cmd" in capsys.readouterr().out
    assert main([*base, "check", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["conflicts"][0]["ref"] == "shared/build_cmd"
    assert main([*base, "cards", "--out", str(tmp_path / "cards")]) == 0
    assert (tmp_path / "cards" / "mem_shared_build_cmd.md").exists()
    assert main([*base, "consolidate", "--as", "coordinator-bot"]) == 0
    assert "posted 2 envelope(s)" in capsys.readouterr().out
    assert main([*base, "consolidate", "--as", "coordinator-bot"]) == 0
    assert "posted 0 envelope(s)" in capsys.readouterr().out
    assert Ledger.from_jsonl(Path(led).read_text()).verify()
