from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger, ProtocolError

from linx import load, owner, selftest
from linx.cli import main
from linx.registry import Registry, RegistryError

REG = load()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in ("UNISYNC_OWNER_KEY", "UNISYNC_OWNER_FROM", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LINX_OWNER_SEED_FILE", str(tmp_path / "keys" / "owner.seed"))
    return tmp_path


def ledger_file(path: Path) -> Path:
    lg = Ledger(clock=ManualClock())
    lg.post("jonny", Kind.MEMORY_WRITE, key="legacy", value=1)  # unsigned history before the pin
    lg.post("o0", Kind.TASK_REQUEST, "T1", title="audit", requires_approval=True)
    lg.post("o0", Kind.TASK_REQUEST, "T2", title="deploy", requires_approval=True)
    path.write_text(lg.to_jsonl())
    return path


def test_seed_path_defaults(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LINX_OWNER_SEED_FILE", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert owner.seed_path() == tmp_path / "LinX" / "owner.seed"
    monkeypatch.delenv("LOCALAPPDATA")
    assert owner.seed_path().parts[-3:] == (".config", "linx", "owner.seed")


def test_registry_owner_key_pin_is_validated() -> None:
    raw = json.loads((Path(__file__).parents[1] / "src/linx/systems.json").read_text())
    pinned = Registry.from_dict({**raw, "owner_key": {"root": "a" * 64, "from": 7}})
    assert (pinned.owner_key, pinned.owner_from) == ("a" * 64, 7)
    for bad, msg in (
        ({"root": "short"}, "64-character"),
        ({"root": "a" * 64, "from": -1}, "non-negative"),
        ("x", "object"),
    ):
        with pytest.raises(RegistryError, match=msg):
            Registry.from_dict({**raw, "owner_key": bad})
    assert REG.owner_key is None


def test_init_refuses_overwrite_and_rotation_resets_state(home: Path) -> None:
    root = owner.init()
    assert len(root) == 64 and owner.load_key().root == root
    with pytest.raises(FileExistsError, match="rotate"):
        owner.init()
    state = owner.seed_path().with_name("owner.state")
    state.write_text('{"max_index": 5}')
    assert owner.init(force=True) != root and not state.exists()


def test_pinned_env_wins_over_registry(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reg = replace(REG, owner_key="b" * 64, owner_from=3)
    assert owner.pinned(reg) == ("b" * 64, 3)
    monkeypatch.setenv("UNISYNC_OWNER_KEY", "c" * 64)
    monkeypatch.setenv("UNISYNC_OWNER_FROM", "9")
    assert owner.pinned(reg) == ("c" * 64, 9)
    assert owner.pinned(None) == ("c" * 64, 9)


def test_ledger_kwargs(home: Path) -> None:
    assert owner.ledger_kwargs(REG) == {}  # nothing pinned: nothing changes
    root = owner.init()
    reg = replace(REG, owner_key=root, owner_from=2)
    kw = owner.ledger_kwargs(reg)
    assert kw["owner_root"] == root and kw["owner_from"] == 2 and callable(kw["signer"])
    assert "signer" not in owner.ledger_kwargs(reg, sign=False)
    wrong = owner.ledger_kwargs(replace(REG, owner_key="d" * 64))["signer"]
    with pytest.raises(ValueError, match="does not match"):
        wrong(b"m", -1)  # checked when it would sign, not on every command
    owner.seed_path().unlink()
    assert "signer" not in owner.ledger_kwargs(reg)  # a reader without the seed still enforces


def test_status_and_selftest_lines(home: Path) -> None:
    assert "checked by name only" in owner.status(REG)[0]
    assert selftest.check_owner(REG).result == selftest.INFO
    root = owner.init()
    assert "not pinned yet" in owner.status(REG)[1]
    reg = replace(REG, owner_key=root)
    assert "matches" in owner.status(reg)[2] and selftest.check_owner(reg).result == selftest.PASS
    other = replace(REG, owner_key="e" * 64)
    assert "DOES NOT match" in owner.status(other)[2] and selftest.check_owner(other).result == selftest.FAIL
    owner.seed_path().write_text("not hex")
    assert "unreadable" in owner.status(reg)[2] and selftest.check_owner(reg).result == selftest.FAIL
    owner.seed_path().unlink()
    assert "cannot sign" in owner.status(reg)[1] or "none at" in owner.status(reg)[2]
    assert selftest.check_owner(reg).result == selftest.INFO


def test_end_to_end_signed_owner_actions(
    home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    led = ledger_file(home / "u.jsonl")
    assert main(["owner", "pin", "--ledger", str(led)]) == 2  # no seed yet
    assert main(["owner", "init"]) == 0
    root = capsys.readouterr().out.split("public owner key: ")[1].split()[0]
    assert main(["owner", "init"]) == 2
    assert main(["owner", "pin", "--ledger", str(led)]) == 0
    out = capsys.readouterr().out
    assert f'"root": "{root}"' in out and '"from": 3' in out and "setx UNISYNC_OWNER_FROM 3" in out
    monkeypatch.setenv("UNISYNC_OWNER_KEY", root)
    monkeypatch.setenv("UNISYNC_OWNER_FROM", "3")
    assert main(["owner", "status"]) == 0 and "matches" in capsys.readouterr().out

    assert main(["approve", "T1", "--ledger", str(led)]) == 0  # this machine holds the seed: signed
    assert main(["halt", "--reason", "drill", "--ledger", str(led)]) == 0
    replay = Ledger.from_jsonl(led.read_text(), owner_root=root, owner_from=3)
    assert replay.status("T1") == "approved" and "sig" in replay.entries[3].body

    monkeypatch.setenv("LINX_OWNER_SEED_FILE", str(home / "elsewhere" / "owner.seed"))  # a machine without it
    assert main(["approve", "T2", "--ledger", str(led)]) == 2
    assert "must be signed" in capsys.readouterr().err
    assert main(["status", "--ledger", str(led), "--root", str(home)]) == 0  # readers still verify fine

    rows = led.read_text().splitlines()
    forged = json.loads(rows[-1])
    forged["body"]["reason"] = "tampered"
    led.write_text("\n".join([*rows[:-1], json.dumps(forged)]) + "\n")
    assert main(["pending", "--ledger", str(led)]) == 2  # a doctored owner row is refused: fail closed
    with pytest.raises(ProtocolError):
        Ledger.from_jsonl(led.read_text(), owner_root=root, owner_from=3)
