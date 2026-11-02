"""Live Vortex -> UniSync relay: poll Vortex's operator event feed and relay new events as they land.

Source: Vortex's `GET /events?after=N&once=1` (operator-only SSE; `once` returns what is there and
closes). Sink: any ledger with `post` and `memory`, normally Lestinxor's `SupabaseLedger` (the shared
`unisync_events` table). Stdlib only.

* The resume point is the ledger's own cursor, so restarts lose and duplicate nothing.
* Network and 5xx errors back off exponentially (capped) and retry; auth errors (401/403) and
  malformed events stop the loop, because retrying can't fix them and skipping would lose data.
* The operator token comes from the environment, never argv. Plain HTTP is allowed only to loopback.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from linx.relay import LedgerLike, RelayError, cursor, relay

log = logging.getLogger("linx.live")

Fetch = Callable[[int], list[dict[str, Any]]]
LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class LiveError(RuntimeError):
    """The relay cannot continue without a person fixing something (credentials, URL, bad data)."""


def parse_sse(text: str) -> list[dict[str, Any]]:
    """Events from an SSE body: one JSON object per `data:` block; comments and keepalives ignored."""
    events: list[dict[str, Any]] = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.split("\n") if line.startswith("data:"))
        if data:
            event = json.loads(data)
            if not isinstance(event, dict):
                raise RelayError("SSE data must be a JSON object")
            events.append(event)
    return events


def check_url(base_url: str) -> str:
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LiveError(f"bad Vortex URL: {base_url!r}")
    if parts.scheme == "http" and parts.hostname not in LOOPBACK:
        raise LiveError("plain http is only allowed to loopback; use https")
    return base_url.rstrip("/")


def http_fetch(base_url: str, token: str, timeout: float = 30.0) -> Fetch:
    """A fetcher for Vortex's `/events?once=1` feed. `base_url` is the server root, e.g. https://vortex.example."""
    root = check_url(base_url)
    if not token:
        raise LiveError("set VORTEX_OPERATOR_TOKEN to an operator's bearer token")

    def fetch(after: int) -> list[dict[str, Any]]:
        url = f"{root}/events?{urllib.parse.urlencode({'after': after, 'once': 1})}"
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return parse_sse(resp.read().decode("utf-8"))

    return fetch


@dataclass
class Stats:
    polls: int = 0
    relayed: int = 0
    errors: int = 0


def run(
    ledger: LedgerLike,
    fetch: Fetch,
    *,
    interval: float = 5.0,
    max_backoff: float = 300.0,
    max_polls: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Stats:
    """Poll and relay until `max_polls` (None = forever). Raises LiveError on unrecoverable faults."""
    stats = Stats()
    delay = interval
    while max_polls is None or stats.polls < max_polls:
        stats.polls += 1
        try:
            sync = getattr(ledger, "sync", None)
            if callable(sync):
                sync()
            n = relay(ledger, fetch(cursor(ledger)))
        except urllib.error.HTTPError as exc:
            if exc.code in (400, 401, 403, 404):
                raise LiveError(f"Vortex refused the request (HTTP {exc.code}); check URL and token") from exc
            n = -1
            log.warning("HTTP %s from Vortex or ledger; retrying in %.0fs", exc.code, delay)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            n = -1
            log.warning("network error (%s); retrying in %.0fs", exc, delay)
        except (RelayError, json.JSONDecodeError) as exc:
            raise LiveError(f"malformed Vortex event, stopping so nothing is skipped: {exc}") from exc
        more = max_polls is None or stats.polls < max_polls
        if n < 0:
            stats.errors += 1
            if more:
                sleep(delay)
            delay = min(delay * 2, max_backoff)
            continue
        delay = interval
        stats.relayed += n
        if n:
            log.info("relayed %d event(s); cursor %d", n, cursor(ledger))
        if more:
            sleep(interval)
    return stats


class FileLedger:
    """A local UniSync ledger in a JSONL file: replayed (and verified) on open, appended line by line."""

    def __init__(
        self, path: Any, *, owner_root: str | None = None, owner_from: int = 0, signer: Any = None
    ) -> None:
        from pathlib import Path

        from lestinxor.unisync import Ledger

        self.path = Path(path)
        text = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        self.ledger = Ledger.from_jsonl(text, owner_root=owner_root, owner_from=owner_from)
        self.ledger.signer = signer

    def post(self, sender: str, kind: str, task_id: str = "", **body: Any) -> Any:
        env = self.ledger.post(sender, kind, task_id, **body)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(env.to_dict(), sort_keys=True, default=str) + "\n")
        return env

    def memory(self, scope: str = "shared") -> dict[str, Any]:
        return dict(self.ledger.memory(scope))
