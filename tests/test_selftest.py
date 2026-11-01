from __future__ import annotations

import urllib.error
from pathlib import Path
from typing import Any

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger, ProtocolError

from linx import load, selftest
from linx.cli import main
from linx.live import LiveError

REG = load()
NAMES = [{"name": n} for n in ("jonny", "lestinxor", "o0", "sebastian", "horizontal", "vortex", "neurocore")]


class FakeRemote:
    def __init__(self, ledger: Ledger, error: Exception | None = None) -> None:
        self.ledger, self.error = ledger, error

    def sync(self) -> int:
        if self.error:
            raise self.error
        return 0


def test_unisync_and_relay_pass(tmp_path: Path) -> None:
    assert selftest.check_unisync(REG).result == selftest.PASS
    step = selftest.check_relay(REG, tmp_path)
    assert step.result == selftest.PASS and "re-run adds nothing" in step.detail


def test_unisync_fails_when_lestinxor_lacks_linx_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    import lestinxor.unisync as u

    monkeypatch.setattr(u, "DEFAULT_AGENTS", tuple(c for c in u.DEFAULT_AGENTS if c.name != "vortex"))
    step = selftest.check_unisync(REG)
    assert step.result == selftest.FAIL and "vortex" in step.detail and "upgrade" in step.fix


def test_checkouts_is_informational(tmp_path: Path) -> None:
    (tmp_path / "Sebastian").mkdir()
    step = selftest.check_checkouts(REG, tmp_path)
    assert (
        step.result == selftest.INFO and step.detail.startswith("1/9") and "missing: lestinxor" in step.detail
    )


def test_vortex_skip_pass_and_failures() -> None:
    assert selftest.check_vortex("", "tok").result == selftest.SKIP
    assert selftest.check_vortex("https://v", "").result == selftest.SKIP
    feed = selftest.SAMPLE_FEED
    ok = selftest.check_vortex("https://v", "t", lambda u, t: lambda after: feed)
    assert ok.result == selftest.PASS and "4 event(s); 4 relayed" in ok.detail

    def refuse(u: str, t: str) -> Any:
        def fetch(after: int) -> Any:
            raise urllib.error.HTTPError(u, 403, "Forbidden", {}, None)  # type: ignore[arg-type]

        return fetch

    bad = selftest.check_vortex("https://v", "t", refuse)
    assert bad.result == selftest.FAIL and "VORTEX_OPERATOR_IDS" in bad.fix

    def down(u: str, t: str) -> Any:
        def fetch(after: int) -> Any:
            raise urllib.error.URLError("refused")

        return fetch

    assert "is Vortex running" in selftest.check_vortex("https://v", "t", down).fix

    def bad_url(u: str, t: str) -> Any:
        raise LiveError("plain http is only allowed to loopback; use https")

    assert "https" in selftest.check_vortex("http://far", "t", bad_url).fix


def test_supabase_pass_and_failures() -> None:
    assert selftest.check_supabase(REG, "", "k").result == selftest.SKIP
    lg = Ledger(clock=ManualClock())
    lg.post("vortex", Kind.HEARTBEAT)
    ok = selftest.check_supabase(REG, "https://s", "k", lambda: FakeRemote(lg), lambda u, k: NAMES)
    assert ok.result == selftest.PASS and "1 entries verify" in ok.detail

    partial = selftest.check_supabase(REG, "https://s", "k", lambda: FakeRemote(lg), lambda u, k: NAMES[:4])
    assert partial.result == selftest.FAIL and "horizontal, neurocore, vortex" in partial.detail
    assert "schema.sql" in partial.fix

    proto = selftest.check_supabase(
        REG,
        "https://s",
        "k",
        lambda: FakeRemote(lg, ProtocolError("unknown sender 'vortex'")),
        lambda u, k: [],
    )
    assert proto.result == selftest.FAIL and "schema.sql" in proto.fix

    lg.entries[0] = lg.entries[0].__class__(**{**lg.entries[0].__dict__, "digest": "0" * 64})
    broken = selftest.check_supabase(REG, "https://s", "k", lambda: FakeRemote(lg), lambda u, k: NAMES)
    assert broken.result == selftest.FAIL and "tampered" in broken.fix


def test_render_verdicts() -> None:
    ok = [selftest.Step("a", selftest.PASS, "x"), selftest.Step("v", selftest.SKIP, "y")]
    assert selftest.render(ok).endswith("LinX works on this machine (not tested: v)")
    bad = [selftest.Step("a", selftest.FAIL, "x", "do this")]
    out = selftest.render(bad)
    assert "fix: do this" in out and out.endswith("1 check(s) failed")


def test_cli_selftest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for var in ("LINX_VORTEX_URL", "VORTEX_OPERATOR_TOKEN", "UNISYNC_SUPABASE_URL", "UNISYNC_SUPABASE_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert main(["selftest", "--root", str(tmp_path)]) == 0
    assert "LinX works on this machine (not tested: vortex, supabase)" in capsys.readouterr().out
    monkeypatch.setenv("VORTEX_OPERATOR_TOKEN", "t")
    assert main(["selftest", "--root", str(tmp_path), "--vortex-url", "http://far.example"]) == 1
    assert "[FAIL] vortex" in capsys.readouterr().out
