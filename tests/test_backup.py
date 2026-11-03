from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger

from linx import backup, notify
from linx.cli import main


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for var in ("UNISYNC_OWNER_KEY", "UNISYNC_OWNER_FROM", "LINX_NTFY_URL", "LINX_BACKUP_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LINX_ANCHORS", str(tmp_path / "anchors.jsonl"))
    monkeypatch.setenv("LINX_OWNER_SEED_FILE", str(tmp_path / "no.seed"))


def ledger(n: int = 3, tag: str = "v") -> Ledger:
    lg = Ledger(clock=ManualClock())
    for i in range(n):
        lg.post("sebastian", Kind.MEMORY_WRITE, key=f"k{i}", value=f"{tag}{i}", scope="shared")
    return lg


def ledger_file(path: Path, n: int = 3) -> Path:
    path.write_text(ledger(n).to_jsonl(), encoding="utf-8")
    return path


# ── backups ──────────────────────────────────────────────────────────────────


def test_write_then_verify_round_trips(tmp_path: Path) -> None:
    lg = ledger()
    made = backup.write(lg, "supabase", out=tmp_path / "b", now=0)
    assert made.path.name == "unisync-19700101T000000Z-2.jsonl"
    m = made.manifest
    assert (m["format"], m["source"], m["count"], m["head_seq"], m["head"]) == (
        backup.FORMAT,
        "supabase",
        3,
        2,
        lg.entries[-1].digest,
    )
    back = backup.verify(made.path)
    assert [e.digest for e in back.entries] == [e.digest for e in lg.entries]
    assert backup.listing(tmp_path / "b")[0].manifest == m


@pytest.mark.parametrize(
    ("damage", "msg"),
    [
        (lambda d, m: d.write_text(d.read_text().replace("v1", "v9")), "SHA-256"),
        (lambda d, m: m.write_text("{"), "not JSON"),
        (lambda d, m: m.write_text(json.dumps({**json.loads(m.read_text()), "format": "x"})), "format"),
        (
            lambda d, m: m.write_text(json.dumps({**json.loads(m.read_text()), "count": 9})),
            "does not match the manifest",
        ),
    ],
)
def test_verify_catches_damage(tmp_path: Path, damage: Any, msg: str) -> None:
    made = backup.write(ledger(), "s", out=tmp_path, now=0)
    damage(made.path, made.path.with_suffix(".json"))
    with pytest.raises(backup.BackupError, match=msg):
        backup.verify(made.path)


def test_verify_without_manifest_still_checks_the_chain(tmp_path: Path) -> None:
    good = ledger_file(tmp_path / "plain.jsonl")
    assert len(backup.verify(good).entries) == 3
    bad = tmp_path / "bad.jsonl"
    bad.write_text(good.read_text().replace("v1", "v9"), encoding="utf-8")
    with pytest.raises(backup.BackupError, match="chain does not verify"):
        backup.verify(bad)
    (tmp_path / "empty.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(backup.BackupError, match="empty"):
        backup.verify(tmp_path / "empty.jsonl")
    with pytest.raises(backup.BackupError, match="no backup"):
        backup.verify(tmp_path / "missing.jsonl")


def test_empty_ledger_is_not_backed_up(tmp_path: Path) -> None:
    with pytest.raises(backup.BackupError, match="empty"):
        backup.write(Ledger(clock=ManualClock()), "s", out=tmp_path)


def test_prune_keeps_the_newest(tmp_path: Path) -> None:
    for t in range(4):
        backup.write(ledger(t + 1), "s", out=tmp_path, now=t * 3600)
    (tmp_path / "unisync-junk.jsonl").write_text("x")
    (tmp_path / "unisync-junk.json").write_text("{")  # unreadable manifest: ignored, never deleted
    removed = backup.prune(2, tmp_path)
    assert [p.name for p in removed] == [
        "unisync-19700101T000000Z-0.jsonl",
        "unisync-19700101T010000Z-1.jsonl",
    ]
    assert [b.manifest["count"] for b in backup.listing(tmp_path)] == [3, 4]
    assert (tmp_path / "unisync-junk.jsonl").exists()
    with pytest.raises(backup.BackupError):
        backup.prune(0, tmp_path)
    assert backup.listing(tmp_path / "nowhere") == []


def test_default_locations(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("LINX_ANCHORS")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert backup.anchors_path() == tmp_path / "LinX" / "anchors.jsonl"
    assert backup.backup_dir() == tmp_path / "LinX" / "backups"
    monkeypatch.setenv("LINX_BACKUP_DIR", str(tmp_path / "bk"))
    assert backup.backup_dir() == tmp_path / "bk"
    monkeypatch.delenv("LOCALAPPDATA")
    assert backup.anchors_path() == Path.home() / ".config" / "linx" / "anchors.jsonl"


# ── anchors ──────────────────────────────────────────────────────────────────


def test_anchor_detects_rollback_and_rewrite(tmp_path: Path) -> None:
    lg = ledger(5)
    a = backup.record(lg.entries, "supabase", now=1)
    assert (a.seq, a.digest) == (4, lg.entries[-1].digest)
    assert backup.check(lg.entries, backup.load_anchors()) == []
    assert backup.check(ledger(7).entries, backup.load_anchors()) == []  # growth is fine

    rolled = backup.check(lg.entries[:3], backup.load_anchors())
    assert len(rolled) == 1 and rolled[0].startswith("rollback: anchor saw #4")
    rewritten = backup.check(ledger(5, tag="x").entries, backup.load_anchors())
    assert len(rewritten) == 1 and rewritten[0].startswith("rewrite: #4")
    # anchors of another source are ignored when a source is given
    assert backup.check(lg.entries[:3], backup.load_anchors(), "other") == []

    with pytest.raises(backup.BackupError, match="rollback"):
        backup.record(lg.entries[:3], "supabase")  # never anchor a rolled-back ledger
    assert len(backup.load_anchors()) == 1
    with pytest.raises(backup.BackupError, match="empty"):
        backup.record([], "supabase")


def test_bad_anchor_file_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "anchors.jsonl"
    path.write_text('{"seq": 1}\n\n', encoding="utf-8")
    with pytest.raises(backup.BackupError, match="anchors.jsonl:1"):
        backup.load_anchors(path)


# ── restore ──────────────────────────────────────────────────────────────────


def test_restore_to_file(tmp_path: Path) -> None:
    made = backup.write(ledger(), "s", out=tmp_path / "b", now=0)
    to = tmp_path / "restored" / "ledger.jsonl"
    backup.restore_file(made.path, to)
    assert Ledger.from_jsonl(to.read_text()).head == made.manifest["head"]
    with pytest.raises(backup.BackupError, match="already exists"):
        backup.restore_file(made.path, to)
    backup.restore_file(made.path, to, force=True)


class FakeSupabase:
    def __init__(self, existing: list[Any]) -> None:
        self.existing = existing
        self.posts: list[list[dict[str, Any]]] = []

    def __call__(self, req: Any, timeout: float) -> Any:
        assert req.full_url.startswith("https://x.supabase.co/rest/v1/unisync_events")
        assert req.get_header("Apikey") == "k"
        if req.get_method() == "GET":
            return io.BytesIO(json.dumps(self.existing).encode())
        self.posts.append(json.loads(req.data))
        return io.BytesIO(b"")


def test_restore_into_empty_supabase_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backup, "BATCH", 2)
    made = backup.write(ledger(5), "s", out=tmp_path, now=0)
    fake = FakeSupabase([])
    backup.restore_supabase(made.path, "https://x.supabase.co", "k", opener=fake)
    assert [[r["seq"] for r in batch] for batch in fake.posts] == [[0, 1], [2, 3], [4]]

    with pytest.raises(backup.BackupError, match="not empty"):
        backup.restore_supabase(made.path, "https://x.supabase.co", "k", opener=FakeSupabase([{"seq": 0}]))
    with pytest.raises(backup.BackupError, match="https"):
        backup.restore_supabase(made.path, "http://x.supabase.co", "k", opener=fake)
    with pytest.raises(backup.BackupError, match="UNISYNC_SUPABASE_URL"):
        backup.restore_supabase(made.path, "", "", opener=fake)


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_backup_check_and_rollback_alarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    led = ledger_file(tmp_path / "ledger.jsonl", n=4)
    out = tmp_path / "bk"
    assert main(["backup", "run", "--ledger", str(led), "--out", str(out), "--keep", "1"]) == 0
    text = capsys.readouterr().out
    assert "backup:" in text and "anchor: #3" in text
    assert main(["backup", "check", "--ledger", str(led)]) == 0
    assert "all 1 anchor(s) hold" in capsys.readouterr().out

    # grow, back up again: the older backup is pruned (keep 1)
    lg = Ledger.from_jsonl(led.read_text())
    lg.post("sebastian", Kind.MEMORY_WRITE, key="k9", value="x", scope="shared")
    led.write_text(lg.to_jsonl(), encoding="utf-8")
    assert main(["backup", "run", "--ledger", str(led), "--out", str(out), "--keep", "1"]) == 0
    assert "pruned 1 old backup(s)" in capsys.readouterr().out
    newest = backup.listing(out)[-1].path

    assert main(["backup", "list", "--out", str(out)]) == 0
    assert "5 entries, head #4" in capsys.readouterr().out
    assert main(["backup", "verify", str(newest)]) == 0
    assert "OK, 5 entries" in capsys.readouterr().out

    # someone deletes the newest rows: check and run both refuse, and the phone gets an urgent push
    pushes: list[notify.Push] = []
    monkeypatch.setenv("LINX_NTFY_URL", "https://ntfy.example/topic")
    monkeypatch.setattr(notify, "send", lambda push, url, token="": pushes.append(push))
    led.write_text("".join(led.read_text().splitlines(keepends=True)[:2]), encoding="utf-8")
    assert main(["backup", "check", "--ledger", str(led)]) == 3
    assert main(["backup", "run", "--ledger", str(led), "--out", str(out)]) == 3
    assert "rollback" in capsys.readouterr().err
    assert len(pushes) == 2 and pushes[0].priority == "urgent"
    assert len(backup.listing(out)) == 1  # no backup of the rolled-back ledger

    # restore the newest good backup over the damaged file
    assert main(["backup", "restore", str(newest), "--to", str(led)]) == 2  # refuses to overwrite
    assert main(["backup", "restore", str(newest), "--to", str(led), "--force"]) == 0
    assert main(["backup", "check", "--ledger", str(led)]) == 0


def test_cli_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["backup", "verify", str(tmp_path / "missing.jsonl")]) == 2
    monkeypatch.delenv("UNISYNC_SUPABASE_URL", raising=False)
    made = backup.write(ledger(), "s", out=tmp_path, now=0)
    assert main(["backup", "restore", str(made.path), "--supabase"]) == 2
    assert "UNISYNC_SUPABASE_URL" in capsys.readouterr().err
    (tmp_path / "anchors.jsonl").write_text("nope\n", encoding="utf-8")
    led = ledger_file(tmp_path / "l.jsonl")
    assert main(["backup", "check", "--ledger", str(led)]) == 2
    bad = tmp_path / "bad.jsonl"
    bad.write_text(led.read_text().replace("v1", "v9"), encoding="utf-8")
    assert main(["backup", "run", "--ledger", str(bad)]) == 2
    assert "cannot open ledger" in capsys.readouterr().err


def test_selftest_reports_backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from linx import selftest

    monkeypatch.setenv("LINX_BACKUP_DIR", str(tmp_path / "bk"))
    assert selftest.check_backups().result == selftest.INFO
    backup.write(ledger(), "s", out=tmp_path / "bk", now=1000)
    backup.record(ledger().entries, "s")
    ok = selftest.check_backups(now=1000 + 86400)
    assert ok.result == selftest.PASS and "1 anchor(s)" in ok.detail
    assert selftest.check_backups(now=1000 + 5 * 86400).result == selftest.FAIL
    (tmp_path / "anchors.jsonl").write_text("bad\n", encoding="utf-8")
    assert selftest.check_backups().result == selftest.FAIL
