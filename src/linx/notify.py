"""Push the morning digest to Jonny's phone via ntfy (https://ntfy.sh, free; or a self-hosted server).

Off unless `LINX_NTFY_URL` is set, e.g. `https://ntfy.sh/<long-random-topic>`. Anyone who knows a public
ntfy topic can read it, so use a long random topic or a server with access control and set
`LINX_NTFY_TOKEN` (sent as a bearer token). Plain http is accepted only for loopback. Stdlib only.
"""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from linx.live import check_url

MAX_BYTES = 3900  # ntfy's message limit is 4096 bytes


@dataclass(frozen=True)
class Push:
    title: str
    body: str
    priority: str  # ntfy: min, low, default, high, urgent
    tags: str


def build(digest_text: str, *, approvals: int, failures: int, halted: bool) -> Push:
    day = digest_text.splitlines()[0].removeprefix("# LinX nightly digest: ") if digest_text else ""
    flags = []
    if halted:
        flags.append("HALTED")
    if approvals:
        flags.append(f"{approvals} to approve")
    if failures:
        flags.append(f"{failures} failed")
    title = f"LinX {day}" + (f": {', '.join(flags)}" if flags else ": all quiet")
    priority = "urgent" if halted else "high" if (approvals or failures) else "default"
    tags = (
        "rotating_light" if halted else "warning" if failures else "bell" if approvals else "white_check_mark"
    )
    body = digest_text.encode("utf-8")
    if len(body) > MAX_BYTES:
        body = body[:MAX_BYTES].rsplit(b"\n", 1)[0] + "\n…(truncated: full digest on the PC)".encode()
    return Push(title, body.decode("utf-8", "ignore"), priority, tags)


Opener = Callable[..., Any]


def send(push: Push, url: str, token: str = "", opener: Opener | None = None, timeout: float = 15.0) -> None:
    target = check_url(url)
    headers = {
        "Title": push.title.encode("ascii", "ignore").decode() or "LinX",
        "Priority": push.priority,
        "Tags": push.tags,
        "Markdown": "yes",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(target, data=push.body.encode("utf-8"), headers=headers, method="POST")
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as resp:
        resp.read()
