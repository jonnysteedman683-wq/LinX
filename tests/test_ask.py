from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger

from linx import ask
from linx.cli import _parser, main

PENDING = [
    ("T1", "audit tasks.py", "o0"),
    ("nightly/d/sebastian/B59", "Sebastian B59: compare runs", "lestinxor"),
]


def parse(argv: list[str]) -> Any:
    return _parser().parse_args(argv)


@pytest.mark.parametrize(
    "text, argv",
    [
        ("what's going on", ["status"]),
        ("how are things", ["status"]),
        ("what's waiting for me", ["pending"]),
        ("stop everything, the build is broken", ["halt", "--reason", "the build is broken"]),
        ("pause please", ["halt", "--reason", "halted via linx ask"]),
        ("unpause", ["resume"]),
        ("start again", ["resume"]),
        ("run the nightly", ["nightly", "run"]),
        ("plan tonight", ["nightly", "plan"]),
        ("what happened overnight", ["nightly", "digest"]),
        ("retry sebastian B59", ["nightly", "retry", "sebastian", "B59"]),
        ("send the report to my phone", ["notify"]),
        ("is it working?", ["selftest"]),
        ("any memory conflicts?", ["memory", "check"]),
        ("what is build_cmd", ["memory", "get", "build_cmd"]),
        ("what's vortex/task/t-aaa", ["memory", "get", "vortex/task/t-aaa"]),
        ("remember goal is ship linx.", ["memory", "put", "goal", "ship linx", "--as", "jonny"]),
        ("which repos are missing", ["doctor"]),
        ("list the systems", ["systems"]),
        ("reject T2 because it's too risky", ["reject", "T2", "--reason", "it's too risky"]),
    ],
)
def test_rules_map_english_to_commands(text: str, argv: list[str]) -> None:
    plan = ask.interpret(text, parse=parse)
    assert plan.argv == argv and plan.source == "rules"


def test_task_resolution_uses_pending_titles() -> None:
    assert ask.from_rules("approve the o0 audit", PENDING).argv == ["approve", "T1"]  # type: ignore[union-attr]
    assert ask.from_rules("approve sebastian's compare runs", PENDING).argv == [  # type: ignore[union-attr]
        "approve",
        "nightly/d/sebastian/B59",
    ]
    assert ask.from_rules("approve nightly/d/sebastian/B59", PENDING).argv[1] == "nightly/d/sebastian/B59"  # type: ignore[union-attr]
    assert ask.from_rules("approve it", PENDING) is None  # ambiguous: no guess
    assert ask.from_rules("approve the o0 audit", []) is None  # no list, no explicit id: no guess


def test_owner_actions_flagged() -> None:
    assert ask.Plan(["halt", "--reason", "x"], "", "rules").owner_action
    assert ask.Plan(["memory", "put", "k", "v", "--as", "jonny"], "", "rules").owner_action
    assert ask.Plan(["nightly", "retry", "s", "B1"], "", "rules").owner_action
    assert not ask.Plan(["status"], "", "rules").owner_action
    assert not ask.Plan(["nightly", "digest"], "", "rules").owner_action
    assert not ask.Plan([], "", "rules").owner_action


@pytest.mark.parametrize(
    "argv, msg",
    [
        (["rm", "-rf", "/"], "not an allowed"),
        (["ask", "status"], "not an allowed"),
        (["relay-live", "--vortex-url", "x"], "not an allowed"),
        ([], "not an allowed"),
        (["status", "--registry", "evil.json"], "--registry"),
        (["approve"], "would not accept"),
        (["nightly", "explode"], "would not accept"),
    ],
)
def test_validate_rejects_anything_off_the_list(argv: list[str], msg: str) -> None:
    with pytest.raises(ask.AskError, match=msg):
        ask.validate(argv, parse)


def test_with_ledger_inserts_after_the_command() -> None:
    assert ask.with_ledger(["nightly", "run"], "u.jsonl") == ["nightly", "--ledger", "u.jsonl", "run"]
    assert ask.with_ledger(["selftest"], "u.jsonl") == ["selftest"]
    assert ask.with_ledger(["status"], None) == ["status"]


class _Resp(io.BytesIO):
    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: object) -> None:
        pass


def model(content: str, seen: dict[str, Any] | None = None) -> Any:
    def opener(req: Any, timeout: float) -> _Resp:
        if seen is not None:
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data)
        return _Resp(json.dumps({"choices": [{"message": {"content": content}}]}).encode())

    return opener


def test_model_fallback_when_no_rule_matches() -> None:
    seen: dict[str, Any] = {}
    plan = ask.interpret(
        "give the o0 thing the green light",
        parse=parse,
        pending=PENDING,
        llm_url="http://127.0.0.1:11434/v1",
        llm_model="qwen2.5:3b",
        opener=model('Sure: {"argv": ["approve", "T1"], "explain": "approve the audit"}', seen),
    )
    assert plan.argv == ["approve", "T1"] and plan.source == "model"
    assert seen["url"] == "http://127.0.0.1:11434/v1/chat/completions"
    assert seen["body"]["model"] == "qwen2.5:3b" and seen["body"]["temperature"] == 0
    system = seen["body"]["messages"][0]["content"]
    assert "Allowed commands" in system and "T1 (audit tasks.py)" in system


@pytest.mark.parametrize(
    "content, msg",
    [
        ('{"argv": ["rm", "-rf", "/"], "explain": "x"}', "not an allowed"),
        ('{"argv": [], "explain": "nothing fits"}', "nothing fits"),
        ("I think you should run status", "did not answer with JSON"),
        ('{"argv": "status"}', "list of strings"),
        ('{"argv": [status]}', "invalid"),
    ],
)
def test_model_output_is_never_trusted(content: str, msg: str) -> None:
    with pytest.raises(ask.AskError, match=msg):
        ask.interpret(
            "do something unusual xyz",
            parse=parse,
            llm_url="http://localhost:11434/v1",
            opener=model(content),
        )


def test_no_model_configured_gives_examples() -> None:
    with pytest.raises(ask.AskError, match="LINX_LLM_URL"):
        ask.interpret("make me a sandwich", parse=parse)


def test_model_url_must_be_https_or_loopback() -> None:
    with pytest.raises(Exception, match="loopback"):
        ask.interpret(
            "do something unusual xyz", parse=parse, llm_url="http://example.com/v1", opener=model("{}")
        )


def test_cli_ask_confirms_and_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    led = tmp_path / "u.jsonl"
    lg = Ledger(clock=ManualClock())
    lg.post("o0", Kind.TASK_REQUEST, "T1", title="audit tasks.py", requires_approval=True)
    led.write_text(lg.to_jsonl())
    base = ["ask", "--ledger", str(led)]
    monkeypatch.delenv("LINX_LLM_URL", raising=False)

    assert main([*base, "--dry-run", "approve the o0 audit"]) == 0
    assert "-> linx approve --ledger" in capsys.readouterr().out
    answers = iter(["y"])  # owner actions need the full word
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    assert main([*base, "approve the o0 audit"]) == 1
    assert "cancelled" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt="": "yes")
    assert main([*base, "approve", "the", "o0", "audit"]) == 0
    assert Ledger.from_jsonl(led.read_text()).status("T1") == "approved"
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    assert main([*base, "what's waiting for me"]) == 1
    assert main([*base, "--yes", "what's waiting for me"]) == 0
    assert "0 waiting for approval" in capsys.readouterr().out
    assert main([*base, "make me a sandwich"]) == 2
    monkeypatch.setenv("LINX_LLM_URL", "http://127.0.0.1:9/v1")  # nothing listening
    assert main([*base, "do something unusual xyz"]) == 2
    assert "local model failed" in capsys.readouterr().err
