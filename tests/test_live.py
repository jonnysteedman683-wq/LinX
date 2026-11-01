from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest
from lestinxor import ManualClock
from lestinxor.unisync import Kind, Ledger

from linx import live
from linx.cli import main
from linx.relay import cursor, tasks

FEED: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "vortex_events.json").read_text()
)


def sse(events: list[dict[str, Any]]) -> str:
    return (
        "".join(f"id: {e['event_id']}\nevent: task\ndata: {json.dumps(e)}\n\n" for e in events)
        + ": keepalive\n\n"
    )


class FakeVortex:
    """Serves FEED in pages, like Vortex's /events?after=N&once=1, with scripted failures."""

    def __init__(self, page: int = 2, failures: list[Exception] | None = None) -> None:
        self.page, self.failures, self.calls = page, list(failures or []), []  # type: ignore[var-annotated]

    def __call__(self, after: int) -> list[dict[str, Any]]:
        self.calls.append(after)
        if self.failures:
            raise self.failures.pop(0)
        return [e for e in FEED if e["event_id"] > after][: self.page]


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, None)  # type: ignore[arg-type]


def test_parse_sse_ignores_comments_and_keepalives() -> None:
    assert live.parse_sse(sse(FEED[:2]).replace("\n", "\r\n")) == FEED[:2]
    assert live.parse_sse(": keepalive\n\n") == []
    with pytest.raises(live.RelayError):
        live.parse_sse("data: [1]\n\n")


@pytest.mark.parametrize("url", ["https://vortex.example", "http://127.0.0.1:8000/", "http://localhost:8000"])
def test_check_url_accepts_https_and_loopback(url: str) -> None:
    assert live.check_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", ["http://vortex.example", "ftp://x", "vortex.example"])
def test_check_url_refuses_plain_http_to_remote(url: str) -> None:
    with pytest.raises(live.LiveError):
        live.check_url(url)


def test_run_relays_page_by_page_and_resumes_from_the_ledger() -> None:
    lg = Ledger(clock=ManualClock())
    vortex = FakeVortex(page=2)
    sleeps: list[float] = []
    stats = live.run(lg, vortex, interval=5, max_polls=4, sleep=sleeps.append)
    assert (stats.polls, stats.relayed, stats.errors) == (4, 6, 0)
    assert vortex.calls == [0, 2, 4, 6]
    assert sleeps == [5, 5, 5]
    assert lg.verify() and cursor(lg) == 6 and tasks(lg)["t-aaa"]["status"] == "completed"


def test_network_errors_back_off_then_recover() -> None:
    lg = Ledger(clock=ManualClock())
    vortex = FakeVortex(page=10, failures=[urllib.error.URLError("down"), http_error(503), TimeoutError()])
    sleeps: list[float] = []
    stats = live.run(lg, vortex, interval=5, max_backoff=15, max_polls=4, sleep=sleeps.append)
    assert (stats.relayed, stats.errors) == (6, 3)
    assert sleeps == [5, 10, 15]


def test_last_poll_never_sleeps() -> None:
    sleeps: list[float] = []
    live.run(
        Ledger(clock=ManualClock()), FakeVortex(failures=[TimeoutError()]), max_polls=1, sleep=sleeps.append
    )
    assert sleeps == []


@pytest.mark.parametrize("code", [401, 403, 404])
def test_auth_and_config_errors_stop(code: int) -> None:
    with pytest.raises(live.LiveError, match=str(code)):
        live.run(Ledger(clock=ManualClock()), FakeVortex(failures=[http_error(code)]), sleep=lambda s: None)


def test_malformed_event_stops_without_posting() -> None:
    lg = Ledger(clock=ManualClock())
    with pytest.raises(live.LiveError, match="malformed"):
        live.run(lg, lambda after: [{"event_id": 1}], max_polls=1, sleep=lambda s: None)
    assert lg.entries == []


def test_http_fetch_sends_operator_token_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class Resp(io.BytesIO):
        def __enter__(self) -> Resp:
            return self

        def __exit__(self, *a: object) -> None:
            pass

    def urlopen(req: Any, timeout: float) -> Resp:
        seen["url"], seen["auth"] = req.full_url, req.get_header("Authorization")
        return Resp(sse(FEED[:1]).encode())

    monkeypatch.setattr(live.urllib.request, "urlopen", urlopen)
    assert live.http_fetch("https://v.example/", "tok")(7) == FEED[:1]
    assert seen == {"url": "https://v.example/events?after=7&once=1", "auth": "Bearer tok"}
    with pytest.raises(live.LiveError, match="VORTEX_OPERATOR_TOKEN"):
        live.http_fetch("https://v.example", "")


def test_file_ledger_appends_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "u.jsonl"
    live.run(live.FileLedger(path), FakeVortex(page=3), max_polls=1, sleep=lambda s: None)
    reopened = live.FileLedger(path)
    assert cursor(reopened) == 3 and reopened.ledger.verify()
    live.run(reopened, FakeVortex(page=10), max_polls=1, sleep=lambda s: None)
    final = Ledger.from_jsonl(path.read_text())
    assert final.verify() and cursor(final) == 6
    assert sum(e.kind is Kind.ESCALATE for e in final.entries) == 1


def test_two_relays_on_one_ledger_do_not_duplicate() -> None:
    lg = Ledger(clock=ManualClock())

    class Racing:
        """Ledger view whose cursor lags: a second relay posting between our read and our post."""

        def __init__(self) -> None:
            self.first = True

        def memory(self, scope: str = "shared") -> dict[str, Any]:
            if self.first:
                self.first = False
                return {}
            return lg.memory(scope)

        def post(self, *a: Any, **k: Any) -> Any:
            return lg.post(*a, **k)

    live.run(lg, FakeVortex(page=10), max_polls=1, sleep=lambda s: None)
    n = len(lg.entries)
    from linx.relay import relay

    assert relay(Racing(), FEED) == 0 and len(lg.entries) == n


def test_cli_relay_live_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("VORTEX_OPERATOR_TOKEN", "tok")
    monkeypatch.setattr(live, "http_fetch", lambda url, token: FakeVortex(page=10))
    out = tmp_path / "u.jsonl"
    assert main(["relay-live", "--vortex-url", "https://v.example", "--ledger", str(out), "--once"]) == 0
    assert "relayed 6 event(s) in 1 poll(s)" in capsys.readouterr().out
    monkeypatch.setattr(live, "http_fetch", lambda url, token: FakeVortex(failures=[http_error(401)]))
    assert main(["relay-live", "--vortex-url", "https://v.example", "--ledger", str(out), "--once"]) == 2
    monkeypatch.setattr(live, "http_fetch", lambda url, token: FakeVortex(failures=[TimeoutError()]))
    assert main(["relay-live", "--vortex-url", "https://v.example", "--ledger", str(out), "--once"]) == 1
